"""
navigator_graph.py
-----------------
The LangGraph graph for the Navigator dimension.
Takes the tab's full conversation so far, injects the current page context,
and uses the writing tool LLM to generate a smart response.
"""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

import configuration

import structlog
log = structlog.get_logger()


# ── State Declaration Update ───────────────────────────────────────────
class NavigatorState(TypedDict):
    messages: Annotated[list, operator.add]
    page_url: str
    page_title: str
    context_snippets: list[str] # Add the snippets parameter to the state array


def _build_turn_text(user_text: str, snippets: list[str]) -> str:
    """
    Folds any attached context snippets directly into THIS turn's human
    message, framed the same way a person would if they pasted text and
    then asked about it in the same breath.

    Why not the system prompt: putting snippets in the system prompt
    frames them as standing background material ("here's some reference
    info, keep it in mind"), not as the thing the user's very next word
    ("this", "that", "summarise this") refers to. Models — like people —
    resolve "this" against what's in front of them in the current turn,
    not against a pile of config-like context sitting above the whole
    conversation. Attaching it to the turn removes the ambiguity that
    was causing "what would you like me to summarize?" replies.
    """
    if not snippets:
        return user_text

    if len(snippets) == 1:
        attached_block = f'--- Attached content ---\n{snippets[0]}\n--- End attached content ---'
    else:
        parts = [f"[{i}] {s}" for i, s in enumerate(snippets, 1)]
        attached_block = "--- Attached content ---\n" + "\n\n".join(parts) + "\n--- End attached content ---"

    # The snippet(s) come first, then the user's own words — mirroring how
    # someone pastes something and then asks about it, so "this"/"it" in
    # the user's text has an unambiguous, immediately-preceding referent.
    return f"{attached_block}\n\n{user_text}"


# ── The Chat Node Logic ─────────────────────────────────────────────────
async def chat_node(state: NavigatorState) -> dict:
    llm = configuration.navigator_general_llm()

    url = state.get("page_url") or "Unknown"
    title = state.get("page_title") or "Unknown"
    snippets = state.get("context_snippets") or []

    # Fold this turn's attached snippets (if any) into the latest human
    # message, rather than parking them in the system prompt. The system
    # prompt stays generic/persona-only; page metadata is still useful
    # ambient context there since it's not something "this" ever refers to.
    messages = list(state["messages"])
    if snippets and messages and isinstance(messages[-1], HumanMessage):
        original_text = messages[-1].content
        messages[-1] = HumanMessage(
            content=_build_turn_text(original_text, snippets)
        )

    num_sources = len(snippets)

    system_prompt = SystemMessage(
        content=(
            "You are a smart, premium, and highly capable assistant embedded in a browser side panel.\n\n"
            "When the user's message includes a block marked "
            "'--- Attached content ---', that block is text they just selected "
            "or dropped in specifically to ask you about — treat it as the "
            "direct subject of their message. If they say things like 'this', "
            "'that', 'summarise this', or ask a question with no other subject, "
            "resolve it against that attached content immediately; do not ask "
            "them to clarify what they mean.\n\n"
            "The attached content may include MULTIPLE sources in the same "
            "message — for example several browser tabs and/or several saved "
            "collections at once. Each source is individually labeled, either "
            "'Tab: \"<page title>\"' or 'Collection: <collection name>', and "
            "when there is more than one attached item they are also numbered "
            "like '[1]', '[2]', etc.\n\n"
            "When more than one source is attached, treat each as its own "
            "independent, unrelated corpus — do not assume they relate to "
            "each other, and never blend their content into a single "
            "narrative. Two attached collections are two separate saved "
            "notebooks the user happens to be asking about at the same "
            "time, not two parts of one document.\n\n"
            "How to answer depends on what the user actually asked:\n"
            "- If they ask something open-ended with no other subject (e.g. "
            "'what is this', 'summarise this'), do NOT just re-list or "
            "transcribe each source's contents back to them one by one — "
            "that's not an answer, it's a dump. Instead, briefly name what "
            "each source IS (by its label) in one short line each, then give "
            "a real synthesized takeaway: what each one is actually about/for "
            "and, if there's nothing connecting them, say plainly that they "
            "look unrelated instead of forcing a link. Keep it tight — a "
            "sentence or two per source, not an exhaustive re-listing of every "
            "item inside it.\n"
            "- If the user's question specifically needs detail from one or "
            "more sources (e.g. 'where is X mentioned', 'which tab talks "
            "about Y', 'list the items in the recipes collection'), then go "
            "into that specific source in the detail the question calls for, "
            "and name the specific tab title or collection name it came "
            "from.\n\n"
            + (
                f"For this message, {num_sources} separate sources are attached — keep "
                f"all {num_sources} clearly distinguished by their labels in your answer.\n\n"
                if num_sources > 1 else ""
            )
            + "Provide helpful, concise, and insightful answers. Use Markdown formatting "
            "whenever deemed necessary to enhance readability, but do not force it. "
            "Keep answers fairly concise — avoid unnecessarily long responses when a "
            "shorter one would fully address the question."
        )
    )

    conversation = [system_prompt] + messages
    response = await llm.ainvoke(conversation)

    return {"messages": [response]}


# ── Build the graph ────────────────────────────────────────────────────
def build_navigator_graph():
    """
    Simplest possible graph shape: chat -> END
    Memory across turns comes from the caller (navigator_bridge.py).
    """
    graph = StateGraph(NavigatorState)
    graph.add_node("chat", chat_node)
    graph.set_entry_point("chat")
    graph.add_edge("chat", END)
    return graph.compile()