import operator
import asyncio
import json
from datetime import datetime
from typing import TypedDict, Annotated, Literal

from pydantic import BaseModel
import aiosqlite

from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import structlog
log = structlog.get_logger()

import tiktoken
from langchain_core.messages import (SystemMessage, HumanMessage, AIMessage, ToolMessage)

from Agent.memory_and_context import get_system_message, get_relevant_preferences
from Agent.tool_manager import ToolManager
import configuration
from Agent.session_store import DB_PATH

# State Class
class AgentState(TypedDict):
    # All messages - HumanMessage, AI Message, ToolMessage
    messages: Annotated[list, operator.add]

    # ── human-in-the-loop patterns ───────────────────────────
    approval_log:      Annotated[list, operator.add]  # every interrupt event
    intent_log:        Annotated[list, operator.add]  # yes/no/edit classifications
    
    # ── tool execution patterns ──────────────────────────────
    tool_call_log:     Annotated[list, operator.add]  # all tool runs + outcomes
    tool_error_log:    Annotated[list, operator.add]  # not-found / failed tools


# Pydantic Schemas
class SafetyResult(BaseModel):
    is_safe: bool


class IntentResult(BaseModel):
    intent: Literal["yes", "no", "edit"]
    refined_instruction: str | None = None

tool_manager = ToolManager()

# globals
graph = None

# tokeniser helpers to count tokens
enc = tiktoken.get_encoding("cl100k_base")
TOKEN_THRESHOLD = 15_000

# Number of recent conversational "units" we try to preserve fresh.
# Actual preserved count may be slightly larger because we keep
# tool-call chains together to avoid corrupting message structure.
KEEP_LAST_MESSAGES = 6

# ─────────────────────────────────────────────────────────────
# Initialize agent
# ─────────────────────────────────────────────────────────────
async def initialize_agent():

    global graph

    safety_llm = configuration.get_safety_llm(SafetyResult)
    intent_llm = configuration.get_intent_llm(IntentResult)
    summarizer_llm = configuration.get_summarizer_llm()

    # ─────────────────────────────────────────────────────────
    # Nodes
    # ─────────────────────────────────────────────────────────
    async def main_node(state: AgentState, config: dict = None) -> AgentState:
        config = config or {}

        """
        PRIMARY AGENT NODE
        ------------------
        Main entry point for the agent's reasoning loop.

        Responsibilities:
        - Trims/summarizes conversation history if needed.
        - Invokes the main LLM with the current message history.
        - Handles two possible outcomes:

            1. No tool calls → Returns a plain AIMessage (final reply).
            Router will forward it to the evaluator.

            2. Tool call detected → 
            - Attaches the AIMessage (with tool_call payload) to the state.
            - Performs an inline safety classification using safety_llm.
            - Stores the safety result in `response.additional_kwargs["safe"]`.
        
        Returns the updated state for the router to process.
        """

        MAIN_LLM_SOUL = get_system_message("main_llm")

        # ── Two-stage tool retrieval ──────────────────────────────────────
        # Stage 1: router LLM picks which servers are needed, AND rewrites
        #          the current turn into a clean self-contained intent string
        # Stage 2: within-server embedding filter, using that rewritten
        #          intent (not raw message concatenation) as the query,
        #          returns only tools clearing the similarity threshold
        user_preferences = await get_relevant_preferences(
            " ".join(
                m.content
                for m in state["messages"][-8:]
                if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str)
            )
        )

        relevant_servers, rewritten_query = await tool_manager.route(state["messages"])

        if relevant_servers is None:
            # Router call itself FAILED (exception/bad output) — this is
            # distinct from "correctly decided no tools are needed" and
            # must never be treated as "give the model everything."
            # Bounded fallback: whatever servers were actually used last
            # turn, or nothing if this is turn one. Never all_tools —
            # dumping every connected server's tools is the exact
            # context-bloat failure mode this routing exists to prevent,
            # and doing it silently on the one turn routing hiccups is
            # the worst possible time for it.
            relevant_servers = _last_used_servers(state["messages"], tool_manager)
            log.warning(
                "router_failed_using_bounded_fallback",
                fallback_servers=relevant_servers,
            )

        relevant_tools = await tool_manager.get_tools_for_servers(
            servers=relevant_servers,
            query=rewritten_query,
            top_k_per_server=6,
        )
        # NOTE: no "if not relevant_tools: relevant_tools = tool_manager.all_tools"
        # fallback here anymore, intentionally. Empty is a valid, correct
        # outcome for pure-conversational turns and for router-failure
        # turns where the bounded fallback above also comes up empty
        # (e.g. very first message in a session).

        # ── Build system message — inject preferences only if something was retrieved ──
        if user_preferences:
            system_content = (
                MAIN_LLM_SOUL
                + "\n\n---\n\n"
                + "# Relevant User Preferences\n\n"
                + "Apply these when deciding how to respond or what to do next:\n\n"
                + user_preferences
            )
        else:
            system_content = MAIN_LLM_SOUL

        auto_approve = config.get("configurable", {}).get("auto_approve", False)
        if auto_approve:
            system_content += (
                "\n\n---\n\n"
                "**BACKGROUND TASK MODE:**\n"
                "You are running as an automated background task. The user is not actively chatting with you.\n"
                "If the user's prompt asks you to monitor for a specific condition (e.g., 'notify me IF there is an email about jobs'), "
                "and that condition is NOT met, you MUST reply with exactly the word: <SILENT>\n"
                "Do not explain that you checked. Do not say 'no new updates'. ONLY output <SILENT>."
            )

        # Rebind with the new tools
        main_llm = configuration.get_main_llm(tools=relevant_tools)
        try:
            trimmed_messages = await maybe_summarize(state["messages"], summarizer_llm)
            response = await main_llm.ainvoke([
                SystemMessage(content=system_content),
                *trimmed_messages
            ])
        except Exception as e:
            return {
                "messages": [AIMessage(content=f"LLM error: {str(e)}")]
            }

        if not response.tool_calls:
            return {"messages": [response]}

        tool_call = response.tool_calls[0]
        tool_name  = tool_call["name"]

        tool_obj  = tool_manager.tool_map.get(tool_name)
        tool_desc = tool_obj.description if tool_obj else "No description available"

        # Check if this thread was flagged for auto-approval
        auto_approve = config.get("configurable", {}).get("auto_approve", False)

        if auto_approve:
            is_safe = True
        else:
            # ── Safety classification ────────────────────────────────────────
            # No prefix fast-path: every tool call goes through safety_llm so
            # that user preferences (surfaced via the system prompt) are the
            # sole authority on auto-execute vs. confirm. This costs one LLM
            # round-trip per tool call, but guarantees no hardcoded rule can
            # silently override what the user configured.
            SAFETY_LLM_SOUL = get_system_message("safety_llm")
            safety_result = await safety_llm.ainvoke([
                SystemMessage(content=SAFETY_LLM_SOUL),
                HumanMessage(content=(
                    f"Tool name: {tool_name}\n"
                    f"Tool description: {tool_desc}\n"
                    f"Args being passed: {tool_call['args']}\n\n"
                    "Is this safe to auto-execute without asking the user?"
                ))
            ])
            is_safe = safety_result.is_safe

        response.additional_kwargs["safe"] = is_safe
        return {"messages": [response]}


    async def human_approval_node(state: AgentState) -> AgentState:
        """
        HUMAN-IN-THE-LOOP APPROVAL NODE
        -------------------------------
        Interrupts execution when the agent wants to call a tool that failed the 
        automatic safety check. Presents the proposed tool call to the user for approval.

        Flow:
        1. Extracts the pending tool call from the last AIMessage.
        2. Triggers an interrupt to get user input.
        3. Uses intent_llm to classify the user's reply (yes / no / edit).
        4. Handles each case:
            - "yes"  → Returns empty messages (router proceeds to tool execution).
            - "no"   → Adds a ToolMessage indicating rejection.
            - "edit" → Adds a ToolMessage with the user's requested changes.

        Returns the updated state so the router can route accordingly.
        """
        
        last      = state["messages"][-1]
        tool_call = last.tool_calls[0]

        # ── Natural Language Formatting ─────────────────────
        tool_name = tool_call["name"]
        raw_args = tool_call.get("args", {})

        # Friendly label we already have in configuration.py
        friendly_title = configuration.TOOL_LABELS.get(tool_name, f"Execute {tool_name}")
        if friendly_title.endswith("..."):
            friendly_title = friendly_title[:-3] # Strip the trailing dots for a cleaner title
            
        # Formatting the arguments as a fenced code block so the Telegram
        # markdown->HTML converter renders them as <pre><code>...</code></pre>
        # instead of a hand-rolled bullet list.
        if raw_args:
            args_json = json.dumps(raw_args, indent=2, default=str, ensure_ascii=False)
            details_section = f"```json\n{args_json}\n```"
        else:
            details_section = ""

        # Presenting it naturally to the user. The yes/no choice is offered
        # via Telegram inline buttons (added on the main.py side); this text
        # just needs to mention that free-text edits are also accepted.
        user_reply = interrupt(
            f"**{friendly_title}**\n\n"
            f"I need your permission to proceed.\n\n"
            f"{details_section}\n\n"
            f"Tap a button below, or reply with what you'd like changed."
        )

        intent_result = await intent_llm.ainvoke([
            SystemMessage(
                content=(
                    "Classify the user's reply to a tool-call approval prompt into yes/no/edit.\n"
                    "IMPORTANT: If the user agrees or confirms — even with extra words like "
                    "'yes please go ahead', 'yeah do it', 'sure remove it', 'yes that's fine' — classify as 'yes'.\n"
                    "Only classify as 'edit' if the user wants to modify the requested action or its parameters before execution.\n"
                    "Only classify as 'no' if the user explicitly cancels or refuses.\n"
                    "If 'edit', populate refined_instruction with what needs to change."
                )
            ),
            HumanMessage(
                content=f"User replied: '{user_reply}'"
            )
        ])

        # ── Populate approval logs ─────────────────────────────
        approval_entry = {
            "tool_name": tool_call["name"],
            "args": tool_call["args"],
            "user_reply": user_reply,
            "timestamp": datetime.utcnow().isoformat()
        }

        intent_entry = {
            "tool_name": tool_call["name"],
            "intent": intent_result.intent,
            "refined_instruction": intent_result.refined_instruction
        }

        if intent_result.intent == "yes":
            return {
                "messages": [],
                "approval_log": [approval_entry],
                "intent_log": [intent_entry]
            }

        content = (
            f"Tool call rejected by user. Reason: {user_reply}"
            if intent_result.intent == "no"
            else f"Tool call not executed. User wants changes: {intent_result.refined_instruction}"
        )
        return {
            "messages": [ToolMessage(tool_call_id=tool_call["id"], content=content)],
            "approval_log": [approval_entry],
            "intent_log": [intent_entry]
        }


    async def tool_executor_node(state: AgentState) -> AgentState:
        """Executes the tool call using whatever is currently in the registry."""
        last = state["messages"][-1]
        tool_call = last.tool_calls[0]

        # Look up the tool by name from the live registry
        tool = tool_manager.tool_map.get(tool_call["name"])

        if not tool:
            timestamp = datetime.utcnow().isoformat()
            error_entry = {
                "tool_name": tool_call["name"],
                "args": tool_call["args"],
                "error": "Tool not found",
                "timestamp": timestamp
            }
            tool_call_entry = {
                "tool_name": tool_call["name"],
                "args": tool_call["args"],
                "status": "not_found",
                "timestamp": timestamp
            }

            return {
                "messages": [ToolMessage(
                    tool_call_id=tool_call["id"],
                    content=f"Tool '{tool_call['name']}' not found. The connector may not be loaded."
                )],
                "tool_call_log": [tool_call_entry],
                "tool_error_log": [error_entry]
            }

        # ── Base tool execution log ────────────────────────────
        tool_call_entry = {
            "tool_name": tool_call["name"],
            "args": tool_call["args"],
            "status": "pending",
            "timestamp": datetime.utcnow().isoformat()
        }

        try:
            # BEFORE EXECUTION
            log.info(
                "Executing tool",
                tool_name=tool_call["name"],
                args=tool_call["args"]
            )

            result = await asyncio.wait_for(tool.ainvoke(tool_call["args"]), timeout=45)

            # AFTER EXECUTION
            log.info(
                "Tool completed with result",
                tool_name=tool_call["name"],
                result=str(result)[:500]
            )

            tool_call_entry["status"] = "success"
            tool_call_entry["result"] = str(result)[:500]

            return {
                "messages": [
                    ToolMessage(
                        tool_call_id=tool_call["id"],
                        content=str(result)
                    )
                ],
                "tool_call_log": [tool_call_entry]
            } 

        except Exception as e:
            log.exception(
                "Tool execution failed",
                tool_name=tool_call["name"],
                args=tool_call["args"]
            )

            tool_call_entry["status"] = "failed"
            tool_call_entry["error"] = str(e)

            error_entry = {
                "tool_name": tool_call["name"],
                "args": tool_call["args"],
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

            return {
                "messages": [
                    ToolMessage(
                        tool_call_id=tool_call["id"],
                        content=f"Tool execution failed.\nError: {str(e)}"
                    )
                ],
                "tool_call_log": [tool_call_entry],
                "tool_error_log": [error_entry]
            }
        

    # ─────────────────────────────────────────────────────────
    # Routers
    # ─────────────────────────────────────────────────────────
    def route_from_main(state: AgentState):
        last = state["messages"][-1]
        if not last.tool_calls:
            return END
        # safety result was stored as a flag inside the AIMessage by main_node
        if last.additional_kwargs.get("safe"):
            return "tools"
        return "human_approval"

    def route_from_approval(state: AgentState):
        last = state["messages"][-1]
        # if last message is a ToolMessage, it means NO or EDIT was handled → back to main
        if isinstance(last, ToolMessage):
            return "main_node"
        # otherwise intent was YES → execute the tool
        return "tools"

    # ─────────────────────────────────────────────────────────
    # Build graph
    # ─────────────────────────────────────────────────────────
    builder = StateGraph(AgentState)

    builder.add_node("main_node",      main_node)
    builder.add_node("tools", tool_executor_node)
    builder.add_node("human_approval", human_approval_node)

    builder.set_entry_point("main_node")

    builder.add_conditional_edges("main_node", route_from_main)
    builder.add_conditional_edges("human_approval", route_from_approval)

    builder.add_edge("tools", "main_node")

    # ── Persistent checkpointer ───────────────────────────────────────────────
    # AsyncSqliteSaver manages its own tables inside the same DB file.
    # It is async-safe and works perfectly with LangGraph's astream_events.
    conn = await aiosqlite.connect(str(DB_PATH))
    checkpointer = AsyncSqliteSaver(conn)
    graph = builder.compile(checkpointer=checkpointer)

    log.info("LangGraph initialized")


# Send message
async def send(message: str, thread_id: str, status_callback=None, cancel_check=None, auto_approve: bool = False):

    config = {
        "configurable": {
            "thread_id": thread_id,
            "auto_approve": auto_approve
        }
    }
    log.info("Thread started", thread_id=thread_id)

    if cancel_check and cancel_check():
        return {"reply": None, "interrupt": None}

    try:
        state = await graph.aget_state(config)

        is_interrupted = (
            bool(state.next)
            and bool(state.tasks)
            and bool(state.tasks[0].interrupts)
        )

        if cancel_check and cancel_check():
            return {"reply": None, "interrupt": None}

        # AUTO RESUME DETECTION
        if is_interrupted:
            log.info("Resuming interrupted thread", message=message, thread_id=thread_id)
            async for event in graph.astream_events(Command(resume=message), config, version="v2"):
                if event["event"] == "on_tool_start" and status_callback:
                    await status_callback(event.get("name", ""))
                elif event["event"] == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                        try:
                            from usage_tracker import record_usage
                            usage = output.usage_metadata
                            model_name = getattr(output, "response_metadata", {}).get("model_name", event.get("name", "unknown"))
                            msg_id = getattr(output, "id", None)
                            record_usage(
                                dimension="agent",
                                session_id=thread_id,
                                model_name=model_name,
                                input_tokens=usage.get("input_tokens", 0),
                                output_tokens=usage.get("output_tokens", 0),
                                cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                                message_id=msg_id
                            )
                        except Exception as rec_err:
                            log.warning("record_usage failed during graph streaming", error=str(rec_err))
            await log_latest_message(config)
        else:
            log.info("User message", message=message, thread_id=thread_id)
            async for event in graph.astream_events({"messages": [HumanMessage(content=message)]}, config, version="v2"):
                if event["event"] == "on_tool_start" and status_callback:
                    await status_callback(event.get("name", ""))
                elif event["event"] == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                        try:
                            from usage_tracker import record_usage
                            usage = output.usage_metadata
                            model_name = getattr(output, "response_metadata", {}).get("model_name", event.get("name", "unknown"))
                            msg_id = getattr(output, "id", None)
                            record_usage(
                                dimension="agent",
                                session_id=thread_id,
                                model_name=model_name,
                                input_tokens=usage.get("input_tokens", 0),
                                output_tokens=usage.get("output_tokens", 0),
                                cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                                message_id=msg_id
                            )
                        except Exception as rec_err:
                            log.warning("record_usage failed during graph streaming", error=str(rec_err))
            await log_latest_message(config)

        if cancel_check and cancel_check():
            return {"reply": None, "interrupt": None}

    except Exception as e:
        log.exception("Graph execution failed", thread_id=thread_id)

        # IMPORTANT: Reset corrupted thread state by starting fresh
        return {
            "reply": (
                "Something went wrong while processing the previous tool call. "
                "Please try again."
            ),
            "interrupt": None,
        }

    # ── Extract latest response ───────────────────────────────
    state = await graph.aget_state(config)
    messages = state.values.get("messages", [])

    latest_ai_message = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            latest_ai_message = msg.content
            break

    # ── Interrupt check ───────────────────────────────────────
    interrupt_message = None
    if (state.next and state.tasks and state.tasks[0].interrupts):
        interrupt_message = state.tasks[0].interrupts[0].value

    return {"reply": latest_ai_message, "interrupt": interrupt_message}


def count_tokens(messages) -> int:
    total = 0
    for msg in messages:
        # Count content
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            total += len(enc.encode(content))
        elif isinstance(content, list):
            # Some tool results come back as list of content blocks
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total += len(enc.encode(block["text"]))

        # Count tool call payloads (missed entirely before)
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                tc_text = f"{tc.get('name', '')} {str(tc.get('args', ''))}"
                total += len(enc.encode(tc_text))

    return total


def message_to_text(m) -> str:

    msg_type = type(m).__name__
    content = getattr(m, "content", "")

    # Tool calls
    if hasattr(m, "tool_calls") and m.tool_calls:

        tool_parts = []

        for tc in m.tool_calls:
            tool_parts.append(
                f"[Tool Call: {tc['name']} | Args: {tc['args']}]"
            )

        return f"{msg_type}: {' '.join(tool_parts)}"

    return f"{msg_type}: {content}"


def _last_used_servers(messages, tool_manager: ToolManager, lookback: int = 20) -> list[str]:
    """
    Bounded fallback for when the server router itself fails (exception,
    not "correctly decided nothing's needed"). Scans recent AIMessage
    tool_calls, maps each tool name back to its owning server via
    tool_manager, and returns the deduped set — NOT the full catalog.

    Deliberately conservative: on a fresh session with no tool-call
    history yet, this returns [] and the turn proceeds with zero tools
    rather than guessing. That's the correct failure mode — a turn with
    no tools is recoverable (user can be asked to clarify or retry);
    a turn silently given every connected server's tools is the exact
    context-bloat / wrong-tool-selection problem routing exists to avoid.
    """
    servers: list[str] = []
    for m in reversed(messages[-lookback:]):
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                server = tool_manager.server_for_tool(tc["name"])
                if server and server not in servers:
                    servers.append(server)
    return servers


def get_safe_fresh_messages(messages, keep_last=KEEP_LAST_MESSAGES):

    """
    Returns a structurally-safe "fresh" tail of messages.

    Why this exists:
    ----------------
    Tool-calling conversations have strict structure rules:

        AIMessage(tool_call)
            ↓
        ToolMessage(result)

    These pairs MUST stay together.

    If summarization cuts between them, OpenAI/LangGraph
    can throw errors like:

        "assistant message with tool_calls must be followed
         by tool messages"

    So instead of blindly slicing the last N messages,
    we walk backwards carefully and preserve complete
    tool interaction chains.
    """

    if len(messages) <= keep_last:
        return messages

    fresh = []
    i = len(messages) - 1

    # Walk backwards through history
    while i >= 0 and len(fresh) < keep_last:

        msg = messages[i]

        # Always include current message
        fresh.append(msg)

        # ---------------------------------------------------------
        # CASE 1: ToolMessage found.
        # Preserve preceding AIMessage(tool_call)
        # ---------------------------------------------------------
        if type(msg).__name__ == "ToolMessage":

            if i - 1 >= 0:

                prev_msg = messages[i - 1]

                has_tool_calls = (hasattr(prev_msg, "tool_calls") and bool(prev_msg.tool_calls))
                if has_tool_calls:
                    fresh.append(prev_msg)
                    i -= 1

        # ---------------------------------------------------------
        # CASE 2: AIMessage(tool_call) found.
        # Preserve following ToolMessage.
        # (Rare edge case protection.)
        # ---------------------------------------------------------
        elif (hasattr(msg, "tool_calls") and bool(msg.tool_calls)):
            if i + 1 < len(messages):

                next_msg = messages[i + 1]

                if type(next_msg).__name__ == "ToolMessage":

                    # Avoid duplicate append
                    if next_msg not in fresh:
                        fresh.append(next_msg)

        i -= 1

    # We walked backwards, so reverse to restore chronology
    fresh.reverse()

    return fresh


async def maybe_summarize(messages, summarizer_llm, token_threshold: int = TOKEN_THRESHOLD, show_log: bool = True):

    token_count = count_tokens(messages)

    if show_log:
        log.info("Context token count", token_count=token_count)

    if token_count < token_threshold:
        return messages

    if show_log:
        log.info("Summarizing old conversation history")

    # ---------------------------------------------------------
    # Preserve a structurally-safe recent window.
    #
    # IMPORTANT:
    # We do NOT blindly slice the last N messages,
    # because that can split:
    #
    #   AI tool call
    #   Tool result
    #
    # which corrupts conversation structure.
    # ---------------------------------------------------------
    fresh = get_safe_fresh_messages(messages, KEEP_LAST_MESSAGES)
    to_summarize = messages[:-len(fresh)]

    history_text = "\n".join(
        message_to_text(m)
        for m in to_summarize
    )

    summary = await summarizer_llm.ainvoke([
        SystemMessage(content=(
            """
            Summarize the conversation briefly while preserving:
            - important context
            - user preferences
            - tool results
            - pending tasks
            - decisions and constraints

            Avoid unnecessary details.
            """
        )),
        HumanMessage(content=history_text)
    ])

    if hasattr(summary, "usage_metadata") and summary.usage_metadata:
        try:
            from usage_tracker import record_usage
            usage = summary.usage_metadata
            model_name = getattr(summary, "response_metadata", {}).get("model_name", "unknown")
            msg_id = getattr(summary, "id", None)
            record_usage(
                dimension="agent",
                session_id="summarizer",
                model_name=model_name,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                message_id=msg_id
            )
        except Exception as rec_err:
            log.warning("record_usage failed for summarizer", error=str(rec_err))

    summary_message = SystemMessage(
        content=(
            "[Conversation Summary]\n"
            f"{summary.content}"
        )
    )

    return [summary_message] + fresh


async def log_latest_message(config):
    state = await graph.aget_state(config)
    messages = state.values.get("messages", [])

    if not messages:
        return

    last = messages[-1]

    msg_type = type(last).__name__

    log.info("Latest message", message_type=msg_type)

    # AI tool calls
    if hasattr(last, "tool_calls") and last.tool_calls:
        for tc in last.tool_calls:
            log.info("Tool call", tool_name=tc["name"], args=tc["args"])

    # Normal content
    if getattr(last, "content", None):
        log.info("Latest message content", content=last.content)
