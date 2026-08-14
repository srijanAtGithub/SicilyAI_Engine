import uuid

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

import configuration
configuration.load_config()

import structlog
from pydantic import BaseModel

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
    action_type: str = "edit"  # New flag: "edit" or "ask"
    history: list[EditHistoryTurn] = Field(default_factory=list)


class EditSelectionResponse(BaseModel):
    edited_text: str


# For Pydantic purposes
class EditResult(BaseModel):
    edited_text: str = Field(
        description="The final output. MUST contain ONLY the rewritten target text. Never include the context before or after."
    )


# ── EDIT SELECTION LOGIC ───────────────────────────────────────
async def process_edit_selection(req: EditSelectionRequest) -> EditSelectionResponse:
    # Pass the action_type down
    edited = await call_edit_model(
        req.selected_text, 
        req.instruction, 
        req.action_type, 
        req.surrounding_context,
        req.history,
    )
    return EditSelectionResponse(edited_text=edited)


async def call_edit_model(
    selected_text: str,
    instruction: str,
    action_type: str = "edit",
    surrounding_context: str = "",
    history: list[EditHistoryTurn] | None = None,
) -> str:
    llm = configuration.navigator_smart_llm(EditResult)
    
    # Branch the persona based on the button clicked
    if action_type == "ask":
        system_msg = SystemMessage(
            content=(
                "You are a precise, direct information assistant. "
                "The user has selected some text and asked a question about it. "
                "Answer their question completely and directly based on the selected text and context. "
                "CRITICAL CONSTRAINT: Output ONLY the direct answer to the user's question. "
                "Do NOT include any conversational filler, pleasantries, meta-commentary, "
                "or follow-up prompts (e.g., never end with 'Let me know if you need more details', "
                "'Hope this helps!', or 'Shall I do anything else?'). "
                "Provide a clean, self-contained final response with absolutely no open-ended transitions."
            )
        ) 
    elif action_type == "rewrite":
        system_msg = SystemMessage(
            content=(
                "You are an automated, programmatic text-replacement engine. "
                "Rewrite the user's selected text in a highly professional manner, completely free of jargon. "
                "The tone should be polished and appropriate for professional emails or personal documents. "
                "CRITICAL CONSTRAINTS: "
                "1. Output EXCLUSIVELY the final revised text. Do NOT include any introductions, explanations, or meta-commentary. "
                "2. If surrounding context is provided, use it ONLY to understand the flow and tone. DO NOT rewrite, include, or repeat the surrounding context in your final output. ONLY replace the selected text."
            )
        )
    elif action_type == "summarise":
        system_msg = SystemMessage(
            content=(
                "You are an automated, programmatic text-replacement engine. "
                "Provide a concise, highly accurate summary of the user's selected text. "
                "CRITICAL CONSTRAINT: Output EXCLUSIVELY the final summarized text. "
                "Do NOT include any introductions like 'Here is the summary', pleasantries, or meta-commentary. "
                "Your entire output will be injected directly into the user's document."
            )
        ) 
    else:
        system_msg = SystemMessage(
            content=(
                "You are an automated, programmatic text-replacement engine. "
                "Rewrite the user's selected text exactly according to their instruction. "
                "CRITICAL CONSTRAINTS: "
                "1. Output EXCLUSIVELY the final revised text. Do NOT include any introductions, explanations, or meta-commentary. "
                "2. If surrounding context is provided, use it ONLY as a background reference. DO NOT rewrite, include, or repeat the surrounding context in your final output. ONLY replace the selected text."
            )
        )
    
    history = history or []

    if not history:
        # First turn: give the model the full selected-text scaffolding,
        # same as before.
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

        prompt_text += f"--- SELECTED TEXT (ONLY REWRITE THIS) ---\n{selected_text}"
    else:
        # Follow-up turn within the same still-open box: the model
        # already has the original text and the prior exchange in
        # `history` below. Sending the full scaffolding again would just
        # duplicate it and waste tokens — a plain instruction is enough.
        prompt_text = instruction

    # Replay this box's prior turns (if any) as real messages so the
    # model has the actual conversation, not just a text blob. This list
    # only ever contains turns from the CURRENT still-open box — the
    # frontend never sends anything left over from a previous selection
    # or a previous session.
    history_messages: list[HumanMessage | AIMessage] = []
    for turn in history:
        if turn.role == "ai":
            history_messages.append(AIMessage(content=turn.text))
        else:
            history_messages.append(HumanMessage(content=turn.text))

    messages = [system_msg, *history_messages, HumanMessage(content=prompt_text)]

    from usage_tracker import record_usage 
    
    edited_text = "" 
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
                    edited_text = data_out.edited_text 
                    
    except Exception as e:
        log.warning("Failed during edit event stream tracking", error=str(e)) 
    
    # Fallback in case the event stream didn't resolve the text correctly 
    if not edited_text: 
        response = await llm.ainvoke(messages) 
        edited_text = response.edited_text 
        
    return edited_text