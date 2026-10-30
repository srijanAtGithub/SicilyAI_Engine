import uuid
from typing import Literal

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
import structlog

import configuration
configuration.load_config()

log = structlog.get_logger()


# WRITING MAJOR TOOL - PYDANTIC SCHEMAS AND DATA MODELS

# A single prior turn from THIS SAME edit box. The frontend keeps this
# array in memory for as long as the floating box stays open and throws
# it away the moment the box closes (Escape, outside click, or a
# successful Replace) — nothing here is ever written to disk or to the
# tab-chat's SQLite history. We accept it as plain role/text pairs
# (mirroring the shape navigator_bridge.py already uses for the chat
# feature) rather than LangChain message objects, since this is the
# wire format and stays framework-agnostic on the JS side.
class EditHistoryTurn(BaseModel):
    role: str  # "user" or "ai"
    text: str


class EditSelectionRequest(BaseModel):
    selected_text: str
    instruction: str
    surrounding_context: str = ""
    action_type: str = "edit"  # "edit" | "ask" | "rewrite" | "summarise"
    history: list[EditHistoryTurn] = Field(default_factory=list)


class EditSelectionResponse(BaseModel):
    edited_text: str
    # "edit"  -> edited_text is a new version of the selection (replaceable)
    # "reply" -> edited_text is a conversational answer (NOT replaceable)
    kind: Literal["edit", "reply"] = "edit"


# Structured output from the model. `kind` comes first on purpose: the
# model commits to "is this an edit or a reply?" before it writes text.
class EditResult(BaseModel):
    kind: Literal["edit", "reply"] = Field(
        description=(
            "'edit' if the user's latest message asks for a (new) version of the selected text; "
            "'reply' if it is an acknowledgement, a question, or anything that is not a request to change the text."
        )
    )
    text: str = Field(
        description=(
            "If kind='edit': ONLY the complete rewritten selected text — never the context before or after, "
            "no introductions or commentary. If kind='reply': a brief, natural conversational reply."
        )
    )


# ── PERSONAS ───────────────────────────────────────────────────

_REPLACEMENT_RULES = (
    "CRITICAL CONSTRAINTS: "
    "1. Output EXCLUSIVELY the final revised text. Do NOT include any introductions, explanations, or meta-commentary. "
    "2. If surrounding context is provided, use it ONLY as a background reference. DO NOT rewrite, include, or "
    "repeat the surrounding context in your final output. ONLY replace the selected text."
)

PERSONAS = {
    "ask": (
        "You are a precise, direct information assistant. "
        "The user has selected some text and asked a question about it. "
        "Answer their question completely and directly based on the selected text and context. "
        "CRITICAL CONSTRAINT: Output ONLY the direct answer to the user's question. "
        "Do NOT include any conversational filler, pleasantries, meta-commentary, "
        "or follow-up prompts (e.g., never end with 'Let me know if you need more details', "
        "'Hope this helps!', or 'Shall I do anything else?'). "
        "Provide a clean, self-contained final response with absolutely no open-ended transitions."
    ),
    "rewrite": (
        "You are an automated, programmatic text-replacement engine. "
        "Rewrite the user's selected text in a highly professional manner, completely free of jargon. "
        "The tone should be polished and appropriate for professional emails or personal documents. "
        + _REPLACEMENT_RULES
    ),
    "summarise": (
        "You are an automated, programmatic text-replacement engine. "
        "Provide a concise, highly accurate summary of the user's selected text. "
        "CRITICAL CONSTRAINT: Output EXCLUSIVELY the final summarized text. "
        "Do NOT include any introductions like 'Here is the summary', pleasantries, or meta-commentary. "
        "Your entire output will be injected directly into the user's document."
    ),
    "edit": (
        "You are an automated, programmatic text-replacement engine. "
        "Rewrite the user's selected text exactly according to their instruction. "
        + _REPLACEMENT_RULES
    ),
}

# Appended only on follow-up turns. Without this, the persona above
# ("output EXCLUSIVELY the revised text") forces every message — even
# "ok" or "thanks" — to be treated as another rewrite request.
FOLLOWUP_RULES_EDIT = (
    "\n\nFOLLOW-UP TURNS: The messages above are your conversation with the user about the selected text. "
    "Decide what the user's LATEST message actually is:\n"
    "- A request to change the text (shorter, more formal, fix X, try again, undo, go back to the original, etc.): "
    "set kind='edit' and put ONLY the complete new version of the selected text in `text`, following the "
    "constraints above. The user's latest instruction overrides any default style described above.\n"
    "- Anything else — an acknowledgement ('ok', 'thanks', 'perfect'), a question about the text or your "
    "changes ('why did you drop the second line?'), or small talk: set kind='reply' and answer naturally "
    "and briefly in `text`, the way a person would. Do NOT produce another version of the text. "
    "A bare acknowledgement gets a one-line reply.\n"
    "The output-only-the-text constraints above apply ONLY when kind='edit'."
)

FOLLOWUP_RULES_ASK = (
    "\n\nFOLLOW-UP TURNS: The messages above are your conversation with the user. Answer their LATEST message. "
    "If it is just an acknowledgement ('ok', 'thanks', 'got it'), reply with a brief, natural one-line "
    "acknowledgement instead of repeating or extending your previous answer. Set kind='reply'."
)


def build_system_message(action_type: str, is_followup: bool) -> SystemMessage:
    persona = PERSONAS.get(action_type, PERSONAS["edit"])
    if is_followup:
        persona += FOLLOWUP_RULES_ASK if action_type == "ask" else FOLLOWUP_RULES_EDIT
    return SystemMessage(content=persona)


def build_first_turn_prompt(instruction: str, selected_text: str, surrounding_context: str) -> str:
    """The full selected-text scaffolding for the opening turn of a box."""
    prompt_text = f"Instruction: {instruction}\n\n"

    # If the selection exists perfectly inside the context, split it apart to remove overlap
    if surrounding_context and selected_text in surrounding_context:
        before, after = surrounding_context.split(selected_text, 1)
        if before.strip():
            prompt_text += f"--- CONTEXT BEFORE ---\n{before.strip()}\n\n"

        prompt_text += f"--- TEXT TO EDIT (REWRITE ONLY THIS) ---\n{selected_text}\n\n"

        if after.strip():
            prompt_text += f"--- CONTEXT AFTER ---\n{after.strip()}\n"

    # Fallback if the string formatting doesn't perfectly match
    elif surrounding_context:
        prompt_text += f"--- BACKGROUND CONTEXT ---\n{surrounding_context.strip()}\n\n"
        prompt_text += f"--- TEXT TO EDIT (REWRITE ONLY THIS) ---\n{selected_text}\n"

    else:
        prompt_text += f"--- TEXT TO EDIT (REWRITE ONLY THIS) ---\n{selected_text}\n"

    return prompt_text


# ── EDIT SELECTION LOGIC ───────────────────────────────────────
async def process_edit_selection(req: EditSelectionRequest) -> EditSelectionResponse:
    text, kind = await call_edit_model(
        req.selected_text,
        req.instruction,
        req.action_type,
        req.surrounding_context,
        req.history,
    )
    return EditSelectionResponse(edited_text=text, kind=kind)


async def call_edit_model(
    selected_text: str,
    instruction: str,
    action_type: str = "edit",
    surrounding_context: str = "",
    history: list[EditHistoryTurn] | None = None,
) -> tuple[str, str]:
    llm = configuration.navigator_smart_llm(EditResult)

    history = history or []
    is_followup = bool(history)
    system_msg = build_system_message(action_type, is_followup)

    # Replay this box's prior turns as real messages. The frontend stores
    # the FIRST user turn as the bare instruction (e.g. "Rewrite this
    # professionally."), so we rebuild it with the full scaffolding here —
    # otherwise the model never sees the original selected text on
    # follow-ups and can't do things like "go back to the original".
    history_messages: list[HumanMessage | AIMessage] = []
    for i, turn in enumerate(history):
        if turn.role == "ai":
            history_messages.append(AIMessage(content=turn.text))
        elif i == 0:
            history_messages.append(HumanMessage(
                content=build_first_turn_prompt(turn.text, selected_text, surrounding_context)
            ))
        else:
            history_messages.append(HumanMessage(content=turn.text))

    if is_followup:
        # Context already lives in history; the new message is sent as-is.
        prompt_text = instruction
    else:
        prompt_text = build_first_turn_prompt(instruction, selected_text, surrounding_context)

    messages = [system_msg, *history_messages, HumanMessage(content=prompt_text)]

    from usage_tracker import record_usage

    result: EditResult | None = None
    session_id = f"edit_{uuid.uuid4().hex[:8]}"

    try:
        # Use astream_events to catch the AIMessage tokens before Pydantic parsing
        async for event in llm.astream_events(messages, version="v2"):

            # 1. Catch the raw LLM usage stats
            if event["event"] == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "usage_metadata") and output.usage_metadata:
                    usage = output.usage_metadata
                    model_name = output.response_metadata.get("model_name", "unknown")

                    try:
                        record_usage(
                            dimension="navigator",
                            session_id=session_id,
                            model_name=model_name,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0)
                        )
                    except Exception as rec_err:
                        log.warning("record_usage failed for edit", error=str(rec_err))

            # 2. Catch the final structured output
            elif event["event"] == "on_chain_end":
                data_out = event.get("data", {}).get("output")
                if isinstance(data_out, EditResult):
                    result = data_out

    except Exception as e:
        log.warning("Failed during edit event stream tracking", error=str(e))

    # Fallback in case the event stream didn't resolve the result correctly
    if result is None:
        result = await llm.ainvoke(messages)

    # Don't trust the model's `kind` where it can only be one thing:
    # "ask" is always a reply, and the opening turn of an edit-type box
    # is always an edit.
    if action_type == "ask":
        kind = "reply"
    elif not is_followup:
        kind = "edit"
    else:
        kind = result.kind

    return result.text, kind