"""
cowork_session.py
----------------
A self-contained terminal chat session for `sicily start`.

Use from:
  - uv build
  - uv pip install dist/sicily-0.2.3-py3-none-any.whl
  - .venv\Scripts\activate // source .venv/bin/activate
  - sicily start
"""

from Cowork.debug_log import DEBUG
from pathlib import Path

def _load_settings():
    """Load configuration for local session."""
    from configuration import load_config
    load_config()

_load_settings()

import asyncio
import operator
import random
import re
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from shared_utils import content_to_text

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.errors import GraphRecursionError
from openai import APIError

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

import configuration

# Initialize the rich console for styling
console = Console()
from Cowork.cowork_tools import LOCAL_TOOLS, _set_sandbox_root, get_friendly_tool_message
from Agent.agent import maybe_summarize
from Cowork.debug_log import reset_step_counter, log_tool_call, log_llm_tokens, log_turn_summary, log_error

log = structlog.get_logger()

BANNER = """
╔═════════════ Sicily v3.1.0 ══════════════╦════════════ What Sicily Cowork Can Do ═════════════╗
║                                          ║                                                    ║
║                                          ║    Sicily can search, inspect, read, organize,     ║
║  Files are sandboxed to this directory.  ║    and safely modify the contents of your          ║
║      Type  exit/quit  to leave.          ║    workspace, including text, code, PDF, Word,     ║
║                                          ║    and Excel documents. Semantic search, file      ║
║                                          ║    discovery, previews, and protected editing      ║
║                                          ║    are available throughout the session.           ║
║                                          ║                                                    ║
╚══════════════════════════════════════════╩════════════════════════════════════════════════════╝
"""

LOCAL_TOKEN_THRESHOLD = 7_000

# Passed to maybe_summarize's additional_agent_specific_system_prompt so
# Sicily investigations don't lose concrete evidence (exact file paths and
# line numbers) to a paraphrased summary — this evidence is expensive to
# re-derive (a fresh search_file_contents call) if it gets vaguened away.
SICILY_SUMMARIZER_ADDENDUM = """
    This conversation is a filesystem investigation. In addition to the
    general summary rules above, preserve the following EXACTLY as found —
    do not paraphrase, round, or approximate them:
    - file paths
    - line numbers (and line ranges) tied to a specific finding
    - exact symbol/function/component names discovered during search

    Prefer a short bulleted list of these concrete facts over flowing prose.
    If the same fact reappears from multiple tool calls, keep it once.
    """

summarizer_llm = configuration.get_summarizer_llm()

# ── Agent state ───────────────────────────────────────────────────────────────
class LocalState(TypedDict):
    messages: Annotated[list, operator.add]


_RATE_LIMIT_MSG_RE = re.compile(r"rate.?limit", re.IGNORECASE)


def _is_rate_limit_error(e: APIError) -> bool:
    """
    True if e is a rate limit, whether it arrived as a proper HTTP 429
    (RateLimitError, has .response / .status_code) or as a mid-stream SSE
    error event. The Responses/Assistants streaming path raises a bare
    APIError with no .response at all in that second case (see
    openai/_streaming.py), so status_code isn't always available and we
    fall back to checking the message/body for a rate-limit signature.
    """
    status_code = getattr(e, "status_code", None)
    if status_code is not None:
        return status_code == 429

    haystack = " ".join(
        str(part) for part in (getattr(e, "message", None), getattr(e, "body", None)) if part
    )
    return bool(_RATE_LIMIT_MSG_RE.search(haystack))


async def _ainvoke_with_retry(llm, messages, max_retries=5, on_retry=None):
    """
    Call llm.ainvoke(messages), retrying on rate limits.

    Honors the provider's own retry-after / retry-after-ms header when
    present (this is what the API tells us to wait, so we trust it over
    a guess), and falls back to exponential backoff with jitter otherwise
    — including the streaming-API case, where a rate limit arrives as a
    bare APIError with no HTTP response/headers attached at all.

    on_retry, if given, is called with (attempt, wait_seconds, max_retries)
    right before each sleep, so callers can surface progress to the user
    (e.g. updating a status spinner) without this helper knowing about UI.
    """
    attempt = 0
    while True:
        try:
            return await llm.ainvoke(messages)
        except APIError as e:
            if not _is_rate_limit_error(e):
                raise

            attempt += 1
            if attempt > max_retries:
                raise

            wait_s = None
            response = getattr(e, "response", None)
            headers = getattr(response, "headers", None) if response else None
            if headers:
                retry_after_ms = headers.get("retry-after-ms")
                retry_after = headers.get("retry-after")
                if retry_after_ms is not None:
                    try:
                        wait_s = float(retry_after_ms) / 1000
                    except (TypeError, ValueError):
                        wait_s = None
                elif retry_after is not None:
                    try:
                        wait_s = float(retry_after)
                    except (TypeError, ValueError):
                        wait_s = None

            if wait_s is None:
                # No usable hint from the server (always true for the
                # streaming SSE path) — back off exponentially, with
                # jitter so concurrent callers don't retry in lockstep.
                wait_s = min(2 ** attempt, 30) + random.uniform(0, 1)

            log.warning(
                "Rate limited, retrying",
                attempt=attempt,
                max_retries=max_retries,
                wait_seconds=round(wait_s, 2),
            )

            if on_retry:
                on_retry(attempt, wait_s, max_retries)

            await asyncio.sleep(wait_s)


# ── Build a minimal LangGraph for local use ───────────────────────────────────
def build_local_graph():
    """
    A simple ReAct-style graph:

        main_node  →  (tool calls?)  →  tool_node  →  main_node
                   ↘  (no tool calls) → END
    """
    main_llm = configuration.get_cowork_llm(tools=LOCAL_TOOLS)

    async def main_node(state: LocalState) -> LocalState:

        system_message = """
            You are Sicily, a local filesystem assistant with access to the user's files through specialized tools.
            Your role is to investigate the filesystem, inspect relevant files, and answer based on evidence rather than assumptions. 
            When information may exist in the user's files, use tools to verify it before responding. 
            Be thorough, accurate, and transparent about what you found and where you found it.
            """

        sandbox_notice = """
            ---
            # Filesystem Access

            You have sandboxed access to the user's local workspace. All paths must be relative—never use absolute paths.

            ## Reading files
            - Explore the workspace before making assumptions.
            - For unknown or potentially large files, inspect only the beginning first before reading the entire file.
            - Prefer targeted reads over loading large files into context.
            - Chain tool calls as needed to gather evidence.
            - Take your time. If what you've found so far doesn't actually answer the
              question, don't settle for it — look elsewhere, go deeper, or try a
              different angle, the way a person would if their first search didn't turn up what they needed.

            ## Writing files
            - Any operation that changes the filesystem requires the user's approval unless they have already explicitly requested that exact change. 
            - For potentially destructive actions (editing, moving, renaming, deleting, or replacing files), always present the preview first when available and wait for confirmation before applying the change. 
            - Respect the sandbox's safety guarantees. Never attempt to bypass them.

            ## General rules
            - Never fabricate file contents or claim to have inspected something you haven't. 
            - If a tool reports an error, relay it honestly instead of guessing. 
            - Prefer the least invasive tool that can answer the user's question.

            ## Response style
            - Default to concise responses. Only go long-form when the user asks for
              detail, or the answer genuinely requires it (e.g. multi-file changes).
            - When you cite a location you found via a tool, state the line number(s)
              exactly as returned — never hedge with "around", "approximately", "roughly",
              or similar. Tool results already give you the real line number; use it as-is.
              Only omit a line number entirely if you genuinely don't have one from evidence
              (e.g. a file-level answer with no specific line) — don't invent an approximate
              one to sound precise.
            """

        # NOTE: summarization is intentionally NOT done here. This node
        # re-runs on every main -> tools -> main cycle within a single
        # user turn, so summarizing here would risk collapsing a
        # ToolMessage away from the AIMessage.tool_calls that produced
        # it — losing the exact evidence the model just gathered mid
        # investigation. Summarization instead happens once, in
        # run_local_session, right when a fresh user message arrives.
        response = await _ainvoke_with_retry(main_llm, [
            SystemMessage(content=system_message + sandbox_notice),
            *state["messages"],
        ])
        return {"messages": [response]}

    def router(state: LocalState):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    tool_node = ToolNode(LOCAL_TOOLS, handle_tool_errors=True)

    graph = StateGraph(LocalState)
    graph.add_node("main", main_node)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("main")
    graph.add_conditional_edges("main", router, {"tools": "tools", END: END})
    graph.add_edge("tools", "main")

    return graph.compile()


# Main session loop
async def run_local_session():
    """Entry point called by `sicily start` in cli.py."""

    # 1. Lock the sandbox to wherever the command was run from
    cwd = Path.cwd().resolve()
    _set_sandbox_root(cwd)

    console.print(f"[bold dark_orange]{BANNER}[/bold dark_orange]")
    print_info(f"Sandbox root: {cwd}")
    console.print()

    # initialise RAG index
    from Cowork.cowork_rag import SicilyRAG, set_rag
    from rich.progress import Progress, SpinnerColumn, BarColumn, MofNCompleteColumn, TextColumn

    # 1. Show something immediately, and find out up front whether there's
    #    actually anything to index — this decides which UI we show below.
    with console.status("[grey50]Preparing index...[/grey50]", spinner="dots", spinner_style="dim"):
        rag = SicilyRAG(cwd)
        pending = rag.count_pending()

    loop = asyncio.get_event_loop()

    if pending == 0:
        # Nothing new or changed. index_session() still has to run — it
        # handles deleted-file cleanup and TF-IDF resync — but none of
        # that is worth a progress bar, so keep it to one quiet spinner.
        with console.status("[grey50]Checking index...[/grey50]", spinner="dots", spinner_style="dim"):
            summary = await loop.run_in_executor(None, rag.index_session)
    else:
        # 2. Real work ahead — determinate bar + single updating
        #    "current file" line. Per-file logging in index_session is
        #    debug-level (see cowork_rag.py) so nothing else writes to
        #    stdout while this Live render is up.
        progress = Progress(
            SpinnerColumn(style="grey50"),
            TextColumn("[grey50]Indexing files[/grey50]"),
            BarColumn(
                style="grey50",
                complete_style="white",
                finished_style="white",
                pulse_style="white",
            ),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[current_file]}[/dim]"),
            console=console,
            transient=True,  # bar clears once done; only the summary line remains
        )

        def _on_index_progress(file_path: Path, current: int, total: int) -> None:
            # Called from the executor thread index_session() runs on.
            # Progress.update() is internally lock-guarded, so this
            # single-writer pattern is safe.
            try:
                rel = file_path.relative_to(cwd)
            except ValueError:
                rel = file_path
            progress.update(task_id, completed=current, total=total, current_file=str(rel))

        with progress:
            task_id = progress.add_task("indexing", total=pending, current_file="")
            summary = await loop.run_in_executor(
                None, lambda: rag.index_session(progress_callback=_on_index_progress)
            )

    set_rag(rag)

    print_info(
        f"Index ready — "
        f"{summary['indexed']} file(s) indexed, "
        f"{summary['skipped']} unchanged, "
        f"{summary['deleted']} removed, "
        f"{summary['failed']} failed"
    )
    console.print()
    # end RAG init

    # 2. Build the graph (no persistence needed for local sessions)
    graph = build_local_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    messages: list = []

    # 3. Chat loop
    while True:
        try:
            user_input = console.input("[white]>>>:[/white] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "bye"):
            print("\nGoodbye!")
            break

        messages.append(HumanMessage(content=user_input))

        # Summarize exactly once per user turn, here — this is the only
        # place a genuinely new user query enters the conversation. Any
        # tool-call chain the model runs *within* this turn is left
        # untouched (see main_node) so evidence it just gathered isn't
        # collapsed away mid-investigation.
        messages = await maybe_summarize(
            messages,
            summarizer_llm,
            token_threshold=LOCAL_TOKEN_THRESHOLD,
            show_log=False,
            additional_agent_specific_system_prompt=SICILY_SUMMARIZER_ADDENDUM,
        )

        # Per-turn call config. recursion_limit counts graph super-steps
        # (main -> tools -> main = 2 steps per tool call), so this budget
        # is set generously in terms of *tool calls*, not raw steps.
        MAX_TOOL_CALLS_PER_TURN = 60
        turn_config = {**config, "recursion_limit": MAX_TOOL_CALLS_PER_TURN * 2 + 5}
        tool_call_count = 0

        # DEBUG: restart the #1, #2, #3... ordering for this fresh turn,
        # and track running totals to print once at the end.
        reset_step_counter()
        turn_llm_calls = 0
        turn_input_tokens = 0
        turn_output_tokens = 0

        try:
            # 1. Start the rich status spinner
            with console.status("[grey50]Thinking...[/grey50]", spinner="dots", spinner_style="dim") as status:
                
                root_run_id = None
                
                # 2. Stream events to catch on_tool_start
                async for event in graph.astream_events({"messages": messages}, turn_config, version="v2"):
                    
                    # Track the root graph run to capture the final output later
                    if root_run_id is None:
                        root_run_id = event.get("run_id")

                    # Track token usage from chat models securely in the background
                    if event["event"] == "on_chat_model_end":
                        output = event.get("data", {}).get("output")
                        if output and hasattr(output, "usage_metadata") and output.usage_metadata:

                            from usage_tracker import record_usage
                            usage = output.usage_metadata
                            model_name = output.response_metadata.get("model_name", event.get("name", "unknown"))
                            
                            record_usage(
                                dimension="cowork",
                                session_id=thread_id,
                                model_name=model_name,
                                input_tokens=usage.get("input_tokens", 0),
                                output_tokens=usage.get("output_tokens", 0),
                                cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0)
                            )

                            # DEBUG: tokens for this single LLM call, plus running total
                            in_tok = usage.get("input_tokens", 0)
                            out_tok = usage.get("output_tokens", 0)
                            cached_tok = usage.get("input_token_details", {}).get("cache_read_tokens", 0)
                            log_llm_tokens(model_name, in_tok, out_tok, cached_tok)
                            turn_llm_calls += 1
                            turn_input_tokens += in_tok
                            turn_output_tokens += out_tok

                    # 3. Intercept tool execution 
                    if event["event"] == "on_tool_start":
                        tool_call_count += 1
                        tool_name = event.get("name")
                        tool_args = event.get("data", {}).get("input", {})

                        # DEBUG: which tool, in what order
                        log_tool_call(tool_name, tool_args)
                        
                        # Format the payload for your helper function
                        tool_call = {"name": tool_name, "args": tool_args}
                        msg = get_friendly_tool_message(tool_call)
                        
                        # Update the terminal spinner with the new message,
                        # and surface progress once things start running long.
                        if tool_call_count > MAX_TOOL_CALLS_PER_TURN * 0.7:
                            status.update(
                                f"[grey50]{msg}... (tool call {tool_call_count}/{MAX_TOOL_CALLS_PER_TURN})[/grey50]"
                            )
                        else:
                            status.update(f"[grey50]{msg}...[/grey50]")

                    elif event["event"] == "on_tool_error":
                        tool_name = event.get("name", "tool")
                        error = event.get("data", {}).get("error", "unknown error")
                        log_error(f"Tool {tool_name} error", error)
                        if DEBUG:
                            status.update(f"[yellow]{tool_name} hit an error: {error}[/yellow]")
                    
                    # 4. Capture the final state when the main graph finishes
                    elif event["event"] == "on_chain_end" and event.get("run_id") == root_run_id:
                        output = event.get("data", {}).get("output")
                        if output and "messages" in output:
                            messages = output["messages"]

            # DEBUG: total tool calls + total tokens for this whole turn
            log_turn_summary(tool_call_count, turn_llm_calls, turn_input_tokens, turn_output_tokens)

            # 5. Find the last AI text response
            reply = None
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    reply = msg.content
                    break

            if reply:
                print_ai(reply)
            else:
                print_ai("(No response)")

        except GraphRecursionError:
            log.warning("Recursion limit hit", tool_calls=tool_call_count)
            # Don't just error out — ask the model to summarize whatever it
            # already found, using the same message history (tool results
            # included), but this time forbid further tool calls.
            messages.append(
                HumanMessage(
                    content=(
                        "You've made a lot of tool calls on this. Please stop "
                        "investigating now and give your best answer based on "
                        "everything you've found so far. If something is still "
                        "unclear, say what's missing and suggest a next step "
                        "instead of guessing."
                    )
                )
            )
            try:
                no_tools_llm = configuration.get_cowork_llm(tools=[])
                trimmed = await maybe_summarize(
                    messages,
                    summarizer_llm,
                    token_threshold=LOCAL_TOKEN_THRESHOLD,
                    show_log=False,
                    additional_agent_specific_system_prompt=SICILY_SUMMARIZER_ADDENDUM,
                )
                final = await no_tools_llm.ainvoke(trimmed)
                if hasattr(final, "usage_metadata") and final.usage_metadata:
                    try:
                        from usage_tracker import record_usage
                        usage = final.usage_metadata
                        model_name = getattr(final, "response_metadata", {}).get("model_name", "unknown")
                        record_usage(
                            dimension="cowork",
                            session_id=thread_id,
                            model_name=model_name,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                            message_id=getattr(final, "id", None)
                        )
                    except Exception as rec_err:
                        log.warning("record_usage failed for cowork recursion fallback", error=str(rec_err))
                messages.append(final)
                print_ai(final.content or "(No response)")
            except Exception as e:
                log_error("Recursion-limit fallback failed", e)
                print_ai(
                    "I ran out of tool-call budget digging into this and couldn't "
                    "wrap up cleanly. Try breaking your question into smaller "
                    "steps, or ask me to continue from where I left off."
                )

        except APIError as e:
            if _is_rate_limit_error(e):
                log.warning("Rate limit exhausted after retries", error=str(e))
                print_ai(
                    "I'm being rate-limited by the model provider right now. "
                    "I retried a few times but it hasn't cleared up yet. "
                    "Wait a moment and try your message again."
                )
            else:
                log.exception("Local session error")
                print_ai(f"Something went wrong: {e}")

        except Exception as e:
            log_error("Local session error", e)
            if DEBUG:
                print_ai(f"Something went wrong: {e}")
            else:
                print_ai("An unexpected error occurred. Please try again later")


# ── Terminal I/O helpers ──────────────────────────────────────────────────────
def print_ai(text: str):
    text = content_to_text(text)
    if not text:
        text = "(No response)"
    md = Markdown(text)
    panel = Panel(md, title="[grey50]Sicily[/grey50]", border_style="grey50", padding=(1, 2), title_align="left")
    console.print()
    console.print(panel)
    console.print()

def print_info(text: str):
    console.print(f"[dim italic]    {text}[/dim italic]")
