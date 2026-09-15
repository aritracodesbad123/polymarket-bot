from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    cx = sqlite3.connect(path, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    return cx


def init_db(cx: sqlite3.Connection) -> None:
    cx.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    now = utcnow()
    cx.execute(
        """INSERT OR IGNORE INTO system_state
           (id, paper_trading_started_at, halted, trading_mode, created_at, updated_at)
           VALUES (1, NULL, 0, 'paper', ?, ?)""",
        (now, now),
    )
    cx.commit()
