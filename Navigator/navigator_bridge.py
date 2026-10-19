"""
navigator_bridge.py
------------------
FastAPI server serving as the browser extension bridge.

Features:
  - Chat (WebSocket): Conversation state scoped by a stable, URL-derived
    session key (see api.js:getSessionKey on the frontend) and persisted
    to a local SQLite database (ChatStore,
    ~/.sicily/Navigator/ChatsData/chats.db), via LangGraph[cite: 3].
    Sessions are created lazily and survive backend restarts AND closing
    the tab (Ctrl+Shift+T reopen restores the same history, since the key
    is the page URL, not Chrome's ephemeral per-session tab id); they're
    only cleared on explicit user request ("Clear chat")[cite: 3].
  - Tools (REST): Stateless endpoints for /edit-selection and /organise-tabs[cite: 3].

Design:
  - This is a modular demo. The ChatStore and internal logic are structured 
    for easy migration to a different persistence backend and integration 
    with LLM configuration/ToolManagers in the future[cite: 3].

Run:
  uv run uvicorn Navigator.navigator_bridge:app --reload --port 8765
"""

import asyncio

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, BaseMessage
from shared_utils import content_to_text

import configuration
configuration.load_config()

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from Navigator.Task_Files.Chat_Section import build_navigator_graph
from Navigator.Task_Files.ChatStore import ChatStore, truncate_messages_at_user_turn

from Navigator.Task_Files.Organise_Tabs import OrganiseTabsRequest, process_organise_tabs
from Navigator.Task_Files.Edit_Selection import EditSelectionRequest, EditSelectionResponse, process_edit_selection
from Navigator.Task_Files.Summarise_Page import SummarisePageRequest, SummarisePageResponse, process_summarise_page
from Navigator.Task_Files.Find_More_Like_This import FindMoreLikeThisRequest, FindMoreLikeThisResponse, process_find_more_like_this
from Navigator.Task_Files.Collections import (
    ListCollectionsResponse,
    AddSnippetRequest,
    AddSnippetResponse,
    CollectionDetailResponse,
    process_list_collections,
    process_add_snippet,
    process_get_collection,
    process_delete_collection,
    process_delete_snippet,
)
from Navigator.Task_Files.Reading_List_Groups import (
    ListReadingListGroupsResponse,
    ReadingListGroupDetailResponse,
    AddReadingListItemRequest,
    AddReadingListItemResponse,
    SetReadRequest,
    process_list_reading_list_groups,
    process_get_reading_list_group,
    process_add_reading_list_item,
    process_set_read,
    process_delete_reading_list_group,
    process_delete_reading_list_item,
)
from Navigator.Task_Files.Site_Theme import (
    GenerateThemeRequest,
    ThemeRecord,
    ListThemesResponse,
    process_generate_theme,
    process_get_theme,
    process_list_themes,
    process_delete_theme,
)

log = structlog.get_logger()

app = FastAPI(title="Sicily Navigator Bridge")

# Dev-only: the popup and background service worker run from a
# chrome-extension:// origin. Extension contexts with host_permissions
# can normally bypass this anyway, but keeping it open here too makes
# the REST endpoints easy to hit directly (curl, a browser tab, etc.)
# while iterating.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/organise-tabs")
async def organise_tabs(req: OrganiseTabsRequest):
    return await process_organise_tabs(req.tabs)

@app.post("/edit-selection", response_model=EditSelectionResponse)
async def edit_selection(req: EditSelectionRequest):
    return await process_edit_selection(req)

@app.post("/summarise-page", response_model=SummarisePageResponse)
async def summarise_page(req: SummarisePageRequest):
    return await process_summarise_page(req)

@app.post("/find-more-like-this", response_model=FindMoreLikeThisResponse)
async def find_more_like_this(req: FindMoreLikeThisRequest):
    return await process_find_more_like_this(req)

# ── Saved Collections ────────────────────────────────────────────────
# Drag-and-drop-to-collections feature: dropping a text snippet on the
# side panel's Collections zone opens a floating picker (existing
# collections, filterable, plus "create and add") which hits these
# routes. See Collections.py for the SQLite-backed storage.

@app.get("/collections", response_model=ListCollectionsResponse)
async def list_collections():
    return await process_list_collections()

@app.get("/collections/{collection_id}", response_model=CollectionDetailResponse)
async def get_collection(collection_id: int):
    try:
        return await process_get_collection(collection_id)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))

@app.delete("/collections/{collection_id}")
async def delete_collection(collection_id: int):
    return await process_delete_collection(collection_id)

@app.delete("/collections/snippets/{snippet_id}")
async def delete_snippet(snippet_id: int):
    return await process_delete_snippet(snippet_id)

@app.post("/collections/add-snippet", response_model=AddSnippetResponse)
async def add_snippet(req: AddSnippetRequest):
    try:
        return await process_add_snippet(req)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

# ── Reading List Groups ──────────────────────────────────────────────
# The '+' hovering over a link-bearing card (Find More Like This results
# today) opens a floating picker — existing reading-list groups,
# filterable, plus "create and add" — mirroring the Collections picker
# above exactly. See Reading_List_Groups.py for the SQLite-backed storage.

@app.get("/reading-list-groups", response_model=ListReadingListGroupsResponse)
async def list_reading_list_groups():
    return await process_list_reading_list_groups()

@app.get("/reading-list-groups/{group_id}", response_model=ReadingListGroupDetailResponse)
async def get_reading_list_group(group_id: int):
    try:
        return await process_get_reading_list_group(group_id)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))

@app.delete("/reading-list-groups/{group_id}")
async def delete_reading_list_group(group_id: int):
    return await process_delete_reading_list_group(group_id)

@app.delete("/reading-list-groups/items/{item_id}")
async def delete_reading_list_item(item_id: int):
    return await process_delete_reading_list_item(item_id)

@app.post("/reading-list-groups/add-item", response_model=AddReadingListItemResponse)
async def add_reading_list_item(req: AddReadingListItemRequest):
    try:
        return await process_add_reading_list_item(req)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

@app.patch("/reading-list-groups/items/{item_id}/read")
async def set_reading_list_item_read(item_id: int, req: SetReadRequest):
    try:
        return await process_set_read(item_id, req.is_read)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))

# ── Site Themes ──────────────────────────────────────────────────────
# AI-designed dark themes per website. The side panel posts a
# locally-measured palette summary (no page content) to /theme/generate;
# the result is stored per hostname in ThemeStore (SQLite) and mirrored
# into chrome.storage.local by the extension so theme_engine.js can apply
# it at document_start. See Site_Theme.py.

@app.post("/theme/generate", response_model=ThemeRecord)
async def generate_theme(req: GenerateThemeRequest):
    try:
        return await process_generate_theme(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/themes", response_model=ListThemesResponse)
async def list_themes():
    return await process_list_themes()

@app.get("/theme/{host}", response_model=ThemeRecord)
async def get_theme(host: str):
    record = await process_get_theme(host)
    if record is None:
        raise HTTPException(status_code=404, detail="No theme saved for this site")
    return record

@app.delete("/theme/{host}")
async def delete_theme(host: str):
    return await process_delete_theme(host)

def _chunk_text(content) -> str:
    """
    Plain text of one streamed model chunk. Chat-completions models give a
    str; Responses-API models (the "smart" capability) give a list of
    content blocks — only the visible text blocks are kept, so reasoning /
    other block types are never streamed to the panel.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, dict)
                and block.get("type") in ("text", "output_text")
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "".join(parts)
    return ""


# One compiled graph instance, reused for every turn on every tab.
# It has no memory of its own — memory lives in the ChatStore below and
# is handed to the graph fresh on each invocation as part of the state.
graph = build_navigator_graph()


# Ephemeral memory for temporary sessions
temp_sessions: dict[str, list[BaseMessage]] = {}


# Persistent, tab-scoped conversation history. Replaces the old
# in-memory SessionStore — same get/set/clear/__len__ shape, but backed
# by SQLite under ~/.sicily/Navigator/ChatsData/chats.db, so history
# survives a backend restart and reopening the same tab. See
# ChatStore.py for the schema and the reasoning behind it.
sessions = ChatStore()


@app.get("/health")
async def health():
    """Quick sanity check — hit this in a navigator tab to confirm the server is up."""
    return {
        "status": "ok",
        "dimension": "navigator",
        "ai": False,
        "active_sessions": len(sessions),
    }


@app.get("/sessions")
async def list_sessions():
    """Returns a summary of every conversation session for the Chats panel."""
    return {"sessions": sessions.list_sessions()}


@app.get("/session/{tab_id}")
async def get_session(tab_id: str):
    """
    Called by the popup on open, so switching back to a tab restores
    that tab's chat instead of showing a blank window.

    NOTE: despite the parameter name (kept for backward path/route
    compatibility), the frontend now sends a stable hash of the page URL
    here (see api.js:getSessionKey), not Chrome's tabId — tabId is
    reassigned by Chrome every browsing session, so keying by it made
    history unreachable after something as ordinary as closing a tab and
    reopening it with Ctrl+Shift+T. This route itself needed no change:
    it already treats the value as an opaque string key.

    Returns each row as {"role": "user"|"ai", "text": str,
    "context_snippets": list[str], "stopped": bool} — the same shape main.js already
    expects from its startup history-replay loop (it calls
    addContextTrail(m.context_snippets) then addMessage(m.text, role)
    for each entry), so drag-dropped context, "Add to Chat" summaries,
    and @-mentioned tab content all reappear exactly as they looked
    when the turn was sent, not just the message text.
    """

    # incognito chats
    if tab_id.startswith("temp_"):
        if tab_id not in temp_sessions:
            return {"messages": []}
        
        formatted_messages = []
        for msg in temp_sessions[tab_id]:
            if isinstance(msg, HumanMessage):
                snippets = msg.additional_kwargs.get("context_snippets", [])
                formatted_messages.append({"role": "user", "text": str(msg.content), "context_snippets": snippets})
            elif isinstance(msg, AIMessage):
                formatted_messages.append({
                    "role": "ai",
                    "text": content_to_text(msg.content),
                    "context_snippets": [],
                    "stopped": bool(msg.additional_kwargs.get("stopped")),
                })
                
        return {"messages": formatted_messages}

    return {"messages": sessions.get_full(tab_id)}


@app.delete("/session/{tab_id}")
async def delete_session(tab_id: str):
    """
    Called only by the popup's explicit "Clear chat" button. background.js
    no longer deletes on tab close (chrome.tabs.onRemoved) — closing a tab
    isn't the user asking to clear anything, and since the key is now the
    page URL rather than the ephemeral tabId, the same "tab" reopened via
    Ctrl+Shift+T should still find its history intact.
    """
    existed = sessions.clear(tab_id)
    return {"status": "cleared", "existed": existed}


class RenameSessionRequest(BaseModel):
    title: str


@app.patch("/session/{tab_id}/title")
async def rename_session(tab_id: str, req: RenameSessionRequest):
    """
    User renames a conversation (Chats panel or the chat header). An empty
    title clears the custom name. The stored name is protected from the
    background auto-titler. Returns the title now in effect.
    """
    title = sessions.rename_session(tab_id, req.title)
    if title is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    return {"status": "renamed", "title": title}


@app.delete("/temp_session/{tab_id}")
async def delete_temp_session(tab_id: str):
    """Called explicitly by the background script when a browser tab closes."""
    if tab_id in temp_sessions:
        del temp_sessions[tab_id]
    return {"status": "cleared"}


@app.websocket("/ws/{tab_id}")
async def websocket_endpoint(websocket: WebSocket, tab_id: str):
    await websocket.accept()
    log.info("Extension connected", tab_id=tab_id)

    # Check if this is an incognito session
    is_temp = tab_id.startswith("temp_")
    if is_temp and tab_id not in temp_sessions:
        temp_sessions[tab_id] = []

    try:
        while True:
            payload = await websocket.receive_json()

            # A stop that arrives when no reply is being generated (the turn
            # already finished) has nothing to cancel.
            if payload.get("type") == "stop":
                continue

            user_text = (payload.get("text") or "").strip()
            page_url = payload.get("page_url") or ""
            page_title = payload.get("page_title") or ""
            capability = payload.get("capability") or "basic"
            
            context_snippets = payload.get("context_snippets") or []

            # Edit-and-resend: the panel sends "truncate_from_turn": N, meaning
            # "drop the Nth user message and everything after it, then treat
            # this text as the next turn". See ChatStore.truncate_from_user_turn.
            truncate_from = payload.get("truncate_from_turn")
            is_edit = (
                isinstance(truncate_from, int)
                and not isinstance(truncate_from, bool)
                and truncate_from >= 0
            )

            log.info("Received message", tab_id=tab_id, text=user_text, page_url=page_url, snippets_count=len(context_snippets))

            if not user_text:
                await websocket.send_json({"reply": "(empty message ignored)"})
                continue

            # Pull history from memory if temp, otherwise from the DB
            if is_temp:
                history = temp_sessions[tab_id]
            else:
                history = sessions.get(tab_id)

            # For an edit, the model must only see the turns BEFORE the edited
            # one. The stored history is trimmed later, only once the graph has
            # succeeded, so a failed LLM call can't leave the chat truncated
            # with nothing in its place.
            if is_edit:
                history = truncate_messages_at_user_turn(history, truncate_from)

            graph_input = {
                "messages": history + [HumanMessage(content=user_text)],
                "page_url": page_url,
                "page_title": page_title,
                "context_snippets": context_snippets, # 2. Forward the snippets into LangGraph state!
                "capability": capability,
            }

            # Stream the reply to the panel token-by-token. "messages" mode
            # yields model chunks (we forward only the chat node's visible
            # text); "values" mode yields the full graph state, and the last
            # one is the same final state graph.ainvoke() used to return —
            # so usage tracking, title generation and persistence below are
            # unchanged. Nothing is saved until the stream completes (or is
            # stopped, see below).
            partial_chunks: list[str] = []

            async def _stream_turn():
                final = None
                async for mode, data in graph.astream(graph_input, stream_mode=["messages", "values"]):
                    if mode == "messages":
                        chunk, meta = data
                        if meta.get("langgraph_node") != "chat" or not isinstance(chunk, AIMessageChunk):
                            continue
                        delta = _chunk_text(chunk.content)
                        if delta:
                            await websocket.send_json({"type": "delta", "text": delta})
                            partial_chunks.append(delta)  # only what the panel actually received
                    elif mode == "values":
                        final = data
                return final

            # Run the generation as a task so we can keep listening to the
            # socket while it streams: a {"type": "stop"} message cancels it,
            # and a client disconnect (WebSocketDisconnect) cancels it too
            # instead of generating into the void.
            stream_task = asyncio.ensure_future(_stream_turn())
            stopped = False
            try:
                while not stream_task.done():
                    recv_task = asyncio.ensure_future(websocket.receive_json())
                    await asyncio.wait({stream_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                    if not recv_task.done():
                        recv_task.cancel()
                        await asyncio.gather(recv_task, return_exceptions=True)
                        continue
                    msg = recv_task.result()  # raises WebSocketDisconnect if the client left
                    if isinstance(msg, dict) and msg.get("type") == "stop" and not stream_task.done():
                        stopped = True
                        break
                    # anything else sent mid-turn is ignored
            finally:
                if not stream_task.done():
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)

            if stopped:
                # Keep what was generated so far; usage for a cancelled call
                # isn't reported, so the tracking loop below sees no messages.
                result = {"messages": []}
            else:
                result = stream_task.result()  # re-raises graph errors -> "Server error" below
                if result is None:
                    raise RuntimeError("Graph finished without producing a result")

            # Track token usage from the returned message state
            try:
                from usage_tracker import record_usage
                for msg in result["messages"]:
                    if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                        usage_meta = msg.usage_metadata
                        model_name = msg.response_metadata.get("model_name", "unknown")
                        msg_id = getattr(msg, "id", None)
                        
                        try:
                            record_usage(
                                dimension="navigator",
                                session_id=tab_id,
                                model_name=model_name,
                                input_tokens=usage_meta.get("input_tokens", 0),
                                output_tokens=usage_meta.get("output_tokens", 0),
                                cached_input_tokens=usage_meta.get("input_token_details", {}).get("cache_read_tokens", 0),
                                message_id=msg_id
                            )
                        except Exception as rec_err:
                            log.warning("record_usage failed for navigator", error=str(rec_err))
            except Exception as token_err:
                log.warning("Failed to collect navigator token metrics", error=str(token_err))

            # Pull the newest AI message out as plain text.
            reply_text = "(no response)"
            for msg in reversed(result["messages"]):
                if isinstance(msg, AIMessage) and msg.content:
                    reply_text = content_to_text(msg.content)
                    break

            if stopped:
                reply_text = "".join(partial_chunks).strip()

            # Persist the truncation now that we have a good reply (or a stop).
            if is_edit:
                if is_temp:
                    temp_sessions[tab_id] = list(history)
                else:
                    removed = sessions.truncate_from_user_turn(tab_id, truncate_from)
                    log.info("Edit-and-resend truncated history", tab_id=tab_id,
                             from_turn=truncate_from, rows_removed=removed)

            if stopped and not reply_text:
                # Stopped before any text was generated: nothing to save.
                await websocket.send_json({"type": "stopped", "reply": "", "saved": False})
                continue

            # Save to memory OR database depending on the session type
            if is_temp:
                # We store context_snippets in additional_kwargs so the GET /session endpoint can restore them
                temp_sessions[tab_id].append(HumanMessage(
                    content=user_text, 
                    additional_kwargs={"context_snippets": context_snippets}
                ))
                temp_sessions[tab_id].append(AIMessage(
                    content=reply_text,
                    additional_kwargs={"stopped": True} if stopped else {},
                ))
            else:
                sessions.append_turn(
                    tab_id=tab_id,
                    user_text=user_text,
                    ai_text=reply_text,
                    context_snippets=context_snippets,
                    stopped=stopped,
                )
            
                # Title generation is now safely nested inside the standard (non-temp) block
                if not history:
                    async def generate_and_save_title():
                        try:
                            llm = configuration.navigator_basic_llm()
                            prompt = (
                                "Generate a chat title based on this first interaction. "
                                "Keep the name very short and concise. Just a few words. "
                                "Do not use quotes or prefixes. Just the title.\n\n"
                                f"User: {user_text}\n\nAI: {reply_text}"
                            )
                            title_msg = await llm.ainvoke(prompt)
                            title = content_to_text(title_msg.content).strip(' "')
                            sessions.set_title(tab_id, title)
                            
                            # Record token usage for title generation
                            try:
                                from usage_tracker import record_usage
                                if hasattr(title_msg, "usage_metadata") and title_msg.usage_metadata:
                                    usage_meta = title_msg.usage_metadata
                                    model_name = title_msg.response_metadata.get("model_name", "unknown")
                                    msg_id = getattr(title_msg, "id", None)
                                    
                                    record_usage(
                                        dimension="navigator",
                                        session_id=tab_id,
                                        model_name=model_name,
                                        input_tokens=usage_meta.get("input_tokens", 0),
                                        output_tokens=usage_meta.get("output_tokens", 0),
                                        cached_input_tokens=usage_meta.get("input_token_details", {}).get("cache_read_tokens", 0),
                                        message_id=msg_id
                                    )
                            except Exception as rec_err:
                                log.warning("record_usage failed for navigator title gen", error=str(rec_err))
                        except Exception as e:
                            log.warning("Failed to generate chat title", error=str(e))
                    
                    asyncio.create_task(generate_and_save_title())

            if stopped:
                # The partial reply was saved as the turn's answer.
                await websocket.send_json({"type": "stopped", "reply": reply_text, "saved": True})
            else:
                await websocket.send_json({"type": "done", "reply": reply_text})

    except WebSocketDisconnect:
        # The popup closing disconnects this socket, but the tab itself is
        # very likely still open — so we deliberately do NOT clear the
        # session here (even for temp sessions, which are now wiped by 
        # background.js tracking the actual browser tab closure).
        log.info("Extension disconnected", tab_id=tab_id)

    except Exception as e:
        log.exception("Bridge error", tab_id=tab_id)
        try:
            await websocket.send_json({"reply": f"Server error: {e}"})
        except Exception:
            pass