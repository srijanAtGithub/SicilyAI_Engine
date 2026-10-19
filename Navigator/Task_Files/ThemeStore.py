"""
ThemeStore.py
-------------
SQLite persistence for AI Site Themes, one row per hostname, at
~/.sicily/Navigator/ThemesData/themes.db (same layout as ChatStore).

This is the source of truth. The extension keeps a mirror of these rows
in chrome.storage.local purely so a theme can be applied at
document_start without a network round-trip — background.js re-syncs
that mirror from here on every extension start/reload, so reloading an
unpacked extension from DevTools never loses anything.
"""

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

DB_PATH = Path.home() / ".sicily" / "Navigator" / "ThemesData" / "themes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_themes (
    host           TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 1,
    original_mode  TEXT NOT NULL,
    target_mode    TEXT NOT NULL,
    anchors_json   TEXT NOT NULL,
    fingerprint    TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'ai',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
)
"""


class ThemeStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "host": row["host"],
            "enabled": bool(row["enabled"]),
            "original_mode": row["original_mode"],
            "target_mode": row["target_mode"],
            "anchors": json.loads(row["anchors_json"] or "[]"),
            "fingerprint": row["fingerprint"],
            "notes": row["notes"],
            "source": row["source"],
            "updated_at": row["updated_at"],
        }

    def get(self, host: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM site_themes WHERE host = ?", (host,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM site_themes ORDER BY updated_at DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def upsert(self, record: dict) -> dict:
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO site_themes
                    (host, enabled, original_mode, target_mode, anchors_json,
                     fingerprint, notes, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    enabled       = excluded.enabled,
                    original_mode = excluded.original_mode,
                    target_mode   = excluded.target_mode,
                    anchors_json  = excluded.anchors_json,
                    fingerprint   = excluded.fingerprint,
                    notes         = excluded.notes,
                    source        = excluded.source,
                    updated_at    = excluded.updated_at
                """,
                (
                    record["host"],
                    1 if record.get("enabled", True) else 0,
                    record["original_mode"],
                    record["target_mode"],
                    json.dumps(record.get("anchors", [])),
                    record.get("fingerprint", ""),
                    record.get("notes", ""),
                    record.get("source", "ai"),
                    now,
                    record.get("updated_at") or now,
                ),
            )
            conn.commit()
        return self.get(record["host"])

    def delete(self, host: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM site_themes WHERE host = ?", (host,))
            conn.commit()
            return cur.rowcount > 0

    def __len__(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM site_themes").fetchone()[0]