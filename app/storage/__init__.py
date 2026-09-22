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


# Added for existing DBs. Fresh databases already have these from schema.sql.
SYSTEM_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ai_calls_utc_day", "TEXT"),
    ("ai_call_count", "INTEGER NOT NULL DEFAULT 0"),
    ("week_started_on", "TEXT"),
    ("week_baseline_equity", "REAL"),
    ("daily_pnl_utc_day", "TEXT"),
    ("daily_realized_pnl", "REAL NOT NULL DEFAULT 0"),
)


def migrate_system_state(cx: sqlite3.Connection) -> None:
    """Add risk-control columns. No-op when they already exist. Does not rewrite rows."""
    have = {row[1] for row in cx.execute("PRAGMA table_info(system_state)").fetchall()}
    if not have:
        return
    for name, decl in SYSTEM_STATE_COLUMNS:
        if name not in have:
            cx.execute(f"ALTER TABLE system_state ADD COLUMN {name} {decl}")


def init_db(cx: sqlite3.Connection) -> None:
    cx.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_system_state(cx)
    now = utcnow()
    cx.execute(
        """INSERT OR IGNORE INTO system_state
           (id, paper_trading_started_at, halted, trading_mode, created_at, updated_at)
           VALUES (1, NULL, 0, 'paper', ?, ?)""",
        (now, now),
    )
    cx.commit()
