from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from agent_platform.config import db_path as _db_path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(path: Path | None = None) -> None:
    p = path or _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT NOT NULL,
              source TEXT,
              source_user TEXT,
              source_context TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              result_detail TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
            CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at DESC);

            CREATE TABLE IF NOT EXISTS conversations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              platform TEXT NOT NULL,
              contact_id TEXT,
              contact_name TEXT,
              direction TEXT NOT NULL,
              content TEXT NOT NULL,
              msg_type TEXT NOT NULL DEFAULT 'text',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_platform_time
              ON conversations(platform, created_at DESC);
            """
        )
        conn.commit()


@contextmanager
def get_conn(path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    p = path or _db_path()
    init_db(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def append_lead_event(lead_id: int, event: dict[str, Any], path: Path | None = None) -> None:
    with get_conn(path) as conn:
        row = conn.execute("SELECT result_detail FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            return
        try:
            arr = json.loads(row["result_detail"] or "[]")
        except json.JSONDecodeError:
            arr = []
        if not isinstance(arr, list):
            arr = []
        arr.append(event)
        now = _utc_now_iso()
        conn.execute(
            "UPDATE leads SET result_detail = ?, updated_at = ? WHERE id = ?",
            (json.dumps(arr, ensure_ascii=False), now, lead_id),
        )
        conn.commit()
