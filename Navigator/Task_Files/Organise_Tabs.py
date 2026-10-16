import uuid

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool

import configuration
configuration.load_config()

import structlog
from pydantic import BaseModel

log = structlog.get_logger()


# ── ORGANISE TABS DATA MODEL & TOOLS ───────────────────────────────────
class TabInfo(BaseModel):
    id: int
    title: str
    url: str
    groupId: int
    pinned: bool = False


class OrganiseTabsRequest(BaseModel):
    tabs: list[TabInfo]


class TabGroupPlan(BaseModel):
    title: str = Field(description="The logical category name for this group of tabs (e.g. 'Social Media', 'Research', 'Shopping', 'Docs'). Keep it short and descriptive.")
    color: str = Field(description="The color of the tab group. Must be one of: 'grey', 'blue', 'red', 'yellow', 'green', 'pink', 'purple', 'cyan', 'orange'.")
    tab_ids: list[int] = Field(description="The list of tab IDs that belong to this group.")


class TabGroupingPlan(BaseModel):
    groups: list[TabGroupPlan] = Field(description="List of tab groups to create.")


@tool(args_schema=TabGroupingPlan)
def organize_tabs_tool(groups: list[TabGroupPlan]) -> list[dict]:
    """
    Organises browser tabs into neat, categorized groups based on their topics, URLs, and titles.
    Use this tool to submit the final tab grouping plan.
    """
    return [g.model_dump() for g in groups]


def _sanitize_groups(groups: list, allowed_ids: set[int]) -> list[dict]:
    """Strip pinned/unknown/duplicate tab IDs from the LLM plan and drop empty groups."""
    clean: list[dict] = []
    seen: set[int] = set()
    for g in groups or []:
        if hasattr(g, "model_dump"):
            g = g.model_dump()
        if not isinstance(g, dict):
            continue
        ids = []
        for tid in g.get("tab_ids", []):
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                continue
            if tid in allowed_ids and tid not in seen:
                ids.append(tid)
                seen.add(tid)
        if ids:
            clean.append({**g, "tab_ids": ids})
    return clean


# ── ORGANISE TABS LOGIC ─────────────────────────────────────────────
async def process_organise_tabs(tabs: list[TabInfo]) -> dict:
    """
    Organise the provided tabs using Sicily AI.
    Exposes a tool-enabled LLM call that groups browser tabs into cohesive categories.
    Pinned tabs are excluded and never included in any group.
    """
    import json
    import re

    # ── Exclude pinned tabs up front ──
    pinned_tabs = [t for t in tabs if t.pinned]
    groupable_tabs = [t for t in tabs if not t.pinned]

    if pinned_tabs:
        log.info("Skipping pinned tabs", pinned_count=len(pinned_tabs))

    if not groupable_tabs:
        log.info("No unpinned tabs to organise")
        return {"groups": []}

    allowed_ids = {t.id for t in groupable_tabs}

    llm = configuration.navigator_smart_llm()

    # Bind the tool to the LLM (and enforce its selection if supported)
    try:
        llm_with_tools = llm.bind_tools([organize_tabs_tool], tool_choice="organize_tabs_tool")
    except Exception:
        llm_with_tools = llm.bind_tools([organize_tabs_tool])

    system_msg = SystemMessage(
        content=(
            "You are an automated, programmatic browser assistant. "
            "Your sole task is to organize the user's browser tabs into logical groups. "
            "**Focus primarily on the tab titles (names).** Tab titles are the strongest indicator of what the user is doing with each tab. "
            "Think like a human organizing their own tabs: look at the specific wording in the titles and group tabs that feel like they belong together for the same task, project, or context. "
            "Be nuanced and practical. Create meaningful, specific groups instead of broad generic ones. "
            "Use both titles and URLs when helpful, but always prioritize the exact tab titles. "
            "For each group, pick a highly professional, **concise** title and a corresponding color. "
            "Only use the tab IDs provided in the input. "
            "CRITICAL: You MUST call the `organize_tabs_tool` with the final grouping plan. "
            "Do NOT return conversational filler or explanations. Only execute the tool call."
        )
    )

    # Only send unpinned tabs to the LLM
    tabs_data = [
        {"id": t.id, "title": t.title, "url": t.url, "groupId": t.groupId}
        for t in groupable_tabs
    ]
    prompt_text = f"Please organize these active browser tabs:\n\n{json.dumps(tabs_data, indent=2)}"

    log.info("Organising tabs with LLM", tab_count=len(groupable_tabs))

    groups = []
    try:
        response = await llm_with_tools.ainvoke([system_msg, HumanMessage(content=prompt_text)])

        # Track token usage from the returned message state
        try:
            from usage_tracker import record_usage
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = response.usage_metadata
                model_name = response.response_metadata.get("model_name", "unknown")
                msg_id = getattr(response, "id", None)
                session_id = f"organise_{uuid.uuid4().hex[:8]}"

                record_usage(
                    dimension="navigator",
                    session_id=session_id,
                    model_name=model_name,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cached_input_tokens=usage.get("input_token_details", {}).get("cache_read_tokens", 0),
                    message_id=msg_id
                )
        except Exception as rec_err:
            log.warning("record_usage failed for organise", error=str(rec_err))

        # 1. Extract grouping plan from tool calls
        if response.tool_calls:
            for tool_call in response.tool_calls:
                if tool_call["name"] == "organize_tabs_tool":
                    args = tool_call["args"]
                    if isinstance(args, dict):
                        groups = args.get("groups", args)
                    elif isinstance(args, list):
                        groups = args

        # 2. Fallback: Parse response content as JSON if no tool calls were correctly captured
        if not groups and response.content:
            json_match = re.search(r"\{.*\}|\[.*\]", response.content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                if isinstance(parsed, dict):
                    groups = parsed.get("groups", parsed)
                elif isinstance(parsed, list):
                    groups = parsed

    except Exception as e:
        log.error("Failed to organise tabs using LLM", error=str(e))
        return {"groups": []}

    # ── Safety net: never return pinned or unknown tab IDs ──
    groups = _sanitize_groups(groups, allowed_ids)

    log.info("Tab grouping plan generated", groups_count=len(groups))
    return {"groups": groups}
