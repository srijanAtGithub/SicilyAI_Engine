"""
ChatStore.py
------------
Persistent conversation history for the Navigator chat, keyed by a
stable session key.

Replaces the old in-memory SessionStore in navigator_bridge.py. Same
public shape (get / set / clear / __len__) so the WebSocket handler
needed almost no changes — but now every human message, AI reply, and
the context_snippets attached to that turn (drag-dropped text, "Add to
Chat" summaries, @-mentioned tab content) survive:

  - a backend restart (uvicorn reload, machine reboot, crash)
  - closing and reopening the exact same tab (Ctrl+Shift+T)

The `tab_id` column name is kept for backward compatibility (schema,
route params, and this class's method signatures all still say
"tab_id"), but the frontend now sends a stable hash of the page URL
here (see api.js:getSessionKey), not Chrome's actual tabId. Chrome
reassigns tabId on every browsing session — including a plain
Ctrl+Shift+T reopen of a just-closed tab — so keying by it made history
for that "same" tab unreachable the moment Chrome handed out a new id.
This class itself needed zero changes for that fix: it always treated
this column as an opaque TEXT key, never cast to int or compared
numerically, so any string works equally well.

Storage lives at:  ~/.sicily/Navigator/ChatsData/chats.db
(SICILY_HOME / "Navigator" / "ChatsData" / "chats.db")

One SQLite file, tab_id as an indexed column. This scales better than
one .db file per tab: SQLite handles many rows in one table fine, and
a single file avoids file-descriptor sprawl and directory-listing cost
as the number of tabs/sessions grows over a browsing session lifetime.

Schema:
    messages(
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        tab_id            TEXT NOT NULL,
        role              TEXT NOT NULL,   -- 'user' | 'ai'
        text              TEXT NOT NULL,
        context_snippets  TEXT,            -- JSON array[str], NULL if none
        created_at        TEXT NOT NULL    -- ISO-8601 UTC
    )
    INDEX (tab_id, id)                     -- fast ordered fetch per tab

`context_snippets` is only ever populated on 'user' rows (context is
attached to the human turn it accompanied, exactly as the UI already
renders it via addContextTrail in ui.js / main.js). The chat graph
itself has no tool-call nodes today (pure text in, text out), so no
tool_calls/tool_results columns exist yet — role is deliberately just
a TEXT column rather than an enum so adding a 'tool' role later is a
data-level change only, not a schema migration.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

SICILY_HOME = Path.home() / ".sicily"
CHATS_DATA_DIR = SICILY_HOME / "Navigator" / "ChatsData"
DB_PATH = CHATS_DATA_DIR / "chats.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tab_id            TEXT NOT NULL,
    role              TEXT NOT NULL,
    text              TEXT NOT NULL,
    context_snippets  TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_tab_id ON messages(tab_id, id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatStore:
    """
    Drop-in replacement for the old in-memory SessionStore.

    Public surface intentionally matches SessionStore exactly:
        get(tab_id)   -> list[BaseMessage]
        set(tab_id, messages) -> None
        clear(tab_id) -> bool
        __len__()     -> int (distinct tab_ids with any history)

    On top of that, get_with_context / set_turn expose the richer shape
    (context_snippets per user turn) that the popup UI needs on reload
    so addContextTrail can be replayed exactly as it was.

    Thread safety: FastAPI's default sync dependency handling and the
    websocket handler here are both effectively single-writer per
    process, but sqlite3 connections aren't safe to share across threads
    by default. A threading.Lock plus check_same_thread=False keeps this
    simple and correct without pulling in a connection pool for what is,
    per the project's own scope, a local single-user demo backend.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ── Rich API (role + text + context_snippets, used by the popup) ──

    def get_full(self, tab_id: str) -> list[dict]:
        """
        Returns every row for this tab in order, as plain dicts:
            {"role": "user"|"ai", "text": str, "context_snippets": list[str]}

        This is what the popup's history load (loadHistory in api.js ->
        GET /session/{tab_id} -> main.js's startup loop) actually needs:
        enough to call addContextTrail(m.context_snippets) then
        addMessage(m.text, role) for every row, identically to a live turn.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT role, text, context_snippets FROM messages "
                "WHERE tab_id = ? ORDER BY id ASC",
                (tab_id,),
            )
            rows = cur.fetchall()

        out = []
        for row in rows:
            snippets = json.loads(row["context_snippets"]) if row["context_snippets"] else []
            out.append({"role": row["role"], "text": row["text"], "context_snippets": snippets})
        return out

    def append_turn(
        self,
        tab_id: str,
        user_text: str,
        ai_text: str,
        context_snippets: list[str] | None = None,
    ) -> None:
        """
        Persist exactly one turn: the human message (with whatever
        context_snippets rode alongside it — drag/drop text, an "Add to
        Chat" summary, an @-mentioned tab's content) and the AI reply
        that followed it.

        Called once per WebSocket turn in navigator_bridge.py, right
        after the graph returns — mirrors the old sessions.set(...) call
        but persists incrementally instead of rewriting the whole
        history array each time.
        """
        snippets_json = json.dumps(context_snippets) if context_snippets else None
        now = _now_iso()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO messages (tab_id, role, text, context_snippets, created_at) "
                "VALUES (?, 'user', ?, ?, ?)",
                (tab_id, user_text, snippets_json, now),
            )
            cur.execute(
                "INSERT INTO messages (tab_id, role, text, context_snippets, created_at) "
                "VALUES (?, 'ai', ?, NULL, ?)",
                (tab_id, ai_text, now),
            )

    # ── LangGraph-shaped API (get/set/clear/__len__, matches old SessionStore) ──

    def get(self, tab_id: str) -> list[BaseMessage]:
        """
        Returns this tab's history as LangChain messages, for handing
        straight into graph.ainvoke({"messages": history + [...] , ...}).

        context_snippets are NOT re-folded into historical HumanMessage
        content here — they were already folded into that turn's AI
        response at the time (chat_node builds the "--- Attached
        content ---" block once, for that turn only), so replaying them
        into every subsequent call would duplicate context the model
        already responded to. They're preserved separately purely for
        the UI's context-trail display on reload (see get_full above).
        """
        rows = self.get_full(tab_id)
        messages: list[BaseMessage] = []
        for row in rows:
            if row["role"] == "user":
                messages.append(HumanMessage(content=row["text"]))
            elif row["role"] == "ai":
                messages.append(AIMessage(content=row["text"]))
        return messages

    def set(self, tab_id: str, messages: list[BaseMessage]) -> None:
        """
        Kept for interface compatibility with the old SessionStore, but
        NOT used by the updated websocket handler (which calls
        append_turn instead, so context_snippets are captured per-turn).

        If ever called directly, this replaces the tab's full history
        with the given messages, dropping any previously stored
        context_snippets (since BaseMessage carries no such field) —
        callers that care about snippet persistence should use
        append_turn instead.
        """
        now = _now_iso()
        with self._cursor() as cur:
            cur.execute("DELETE FROM messages WHERE tab_id = ?", (tab_id,))
            for m in messages:
                if isinstance(m, HumanMessage):
                    role = "user"
                elif isinstance(m, AIMessage):
                    role = "ai"
                else:
                    continue
                cur.execute(
                    "INSERT INTO messages (tab_id, role, text, context_snippets, created_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (tab_id, role, m.content, now),
                )

    def clear(self, tab_id: str) -> bool:
        """Returns True if any rows existed for this tab_id and were removed."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM messages WHERE tab_id = ? LIMIT 1", (tab_id,))
            existed = cur.fetchone() is not None
            cur.execute("DELETE FROM messages WHERE tab_id = ?", (tab_id,))
        return existed

    def __len__(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT tab_id) FROM messages")
            (count,) = cur.fetchone()
        return count

    def list_sessions(self) -> list[dict]:
        """
        Returns a summary of every distinct session (tab_id) that has
        at least one message, ordered by most recently active first.

        Each entry:
            {
                "session_key": str,
                "preview":     str,   -- first user message (truncated)
                "last_active": str,   -- ISO-8601 UTC of newest message
            }
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    tab_id,
                    MAX(created_at)     AS last_active
                FROM messages
                GROUP BY tab_id
                ORDER BY last_active DESC
                """
            )
            rows = cur.fetchall()

        sessions: list[dict] = []
        for row in rows:
            tab_id = row["tab_id"]
            # Grab the first user message as a preview
            with self._cursor() as cur:
                cur.execute(
                    "SELECT text FROM messages "
                    "WHERE tab_id = ? AND role = 'user' "
                    "ORDER BY id ASC LIMIT 1",
                    (tab_id,),
                )
                first = cur.fetchone()

            preview = ""
            if first:
                preview = first["text"][:120]

            sessions.append({
                "session_key": tab_id,
                "preview": preview,
                "last_active": row["last_active"],
            })

        return sessions

    def close(self) -> None:
        with self._lock:
            self._conn.close()