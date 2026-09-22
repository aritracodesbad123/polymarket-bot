from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.storage.db import Database, DatabaseError
from app.storage.models import SystemState
from app.storage import utcnow


def _require(row: sqlite3.Row, name: str):
    if name not in row.keys():
        raise DatabaseError(f"system_state_missing_column:{name}")
    return row[name]


class Repositories:
    def __init__(self, db: Database) -> None:
        self.db = db

    def state(self) -> SystemState:
        row = self.db.query_one("SELECT * FROM system_state WHERE id = 1")
        if row is None:
            now = utcnow()
            self.db.execute(
                """INSERT INTO system_state (id, halted, trading_mode, created_at, updated_at)
                   VALUES (1, 0, 'paper', ?, ?)""",
                (now, now),
            )
            return SystemState()
        return SystemState(
            paper_trading_started_at=row["paper_trading_started_at"],
            halted=bool(row["halted"]),
            halt_reason=row["halt_reason"],
            trading_mode=row["trading_mode"],
            live_activated_at=row["live_activated_at"],
            consecutive_losses=int(row["consecutive_losses"] or 0),
            ai_calls_utc_day=_require(row, "ai_calls_utc_day"),
            ai_call_count=int(_require(row, "ai_call_count") or 0),
            week_started_on=_require(row, "week_started_on"),
            week_baseline_equity=(
                None
                if _require(row, "week_baseline_equity") is None
                else float(row["week_baseline_equity"])
            ),
            daily_pnl_utc_day=_require(row, "daily_pnl_utc_day"),
            daily_realized_pnl=float(_require(row, "daily_realized_pnl") or 0.0),
        )

    def mark_paper_started(self) -> None:
        st = self.state()
        if st.paper_trading_started_at:
            return
        now = utcnow()
        self.db.execute(
            "UPDATE system_state SET paper_trading_started_at=?, updated_at=? WHERE id=1",
            (now, now),
        )
        self.event("PAPER_MODE", "paper trading clock started")

    def halt(self, reason: str) -> None:
        now = utcnow()
        self.db.execute(
            "UPDATE system_state SET halted=1, halt_reason=?, updated_at=? WHERE id=1",
            (reason, now),
        )
        self.event("KILL_SWITCH", reason)

    def resume_paper(self) -> None:
        now = utcnow()
        self.db.execute(
            """UPDATE system_state SET halted=0, halt_reason=NULL, trading_mode='paper',
               updated_at=? WHERE id=1""",
            (now,),
        )
        self.event("PAPER_MODE", "operator resumed paper")

    def set_live_activated(self, ts: str) -> None:
        now = utcnow()
        self.db.execute(
            """UPDATE system_state SET live_activated_at=?, trading_mode='live',
               updated_at=? WHERE id=1""",
            (ts, now),
        )

    def set_consecutive_losses(self, n: int) -> None:
        self.db.execute(
            "UPDATE system_state SET consecutive_losses=?, updated_at=? WHERE id=1",
            (n, utcnow()),
        )

    def _state_row(self) -> sqlite3.Row:
        self.state()
        row = self.db.query_one("SELECT * FROM system_state WHERE id = 1")
        if row is None:
            raise DatabaseError("system_state_missing")
        return row

    def _update_state(self, sql: str, params: tuple) -> None:
        self.db.execute(sql, params)
        row = self.db.query_one("SELECT id FROM system_state WHERE id = 1")
        if row is None:
            raise DatabaseError("system_state_update_failed")

    def ai_burn_state(self) -> tuple[str | None, int]:
        row = self._state_row()
        day = _require(row, "ai_calls_utc_day")
        count = _require(row, "ai_call_count")
        return (None if day is None else str(day), int(count or 0))

    def set_ai_burn(self, day: str, count: int) -> None:
        if not day or count < 0:
            raise DatabaseError("ai_burn_invalid")
        self._update_state(
            """UPDATE system_state SET ai_calls_utc_day=?, ai_call_count=?, updated_at=?
               WHERE id=1""",
            (day, int(count), utcnow()),
        )

    def week_baseline_state(self) -> tuple[str | None, float | None]:
        row = self._state_row()
        started = _require(row, "week_started_on")
        base = _require(row, "week_baseline_equity")
        return (
            None if started is None else str(started),
            None if base is None else float(base),
        )

    def set_week_baseline(self, started_on: str, equity: float) -> None:
        if not started_on or equity < 0:
            raise DatabaseError("week_baseline_invalid")
        self._update_state(
            """UPDATE system_state SET week_started_on=?, week_baseline_equity=?, updated_at=?
               WHERE id=1""",
            (started_on, float(equity), utcnow()),
        )

    def daily_realized_state(self) -> tuple[str | None, float]:
        row = self._state_row()
        day = _require(row, "daily_pnl_utc_day")
        pnl = _require(row, "daily_realized_pnl")
        return (None if day is None else str(day), float(pnl or 0.0))

    def set_daily_realized(self, day: str, pnl: float) -> None:
        if not day:
            raise DatabaseError("daily_realized_invalid")
        self._update_state(
            """UPDATE system_state SET daily_pnl_utc_day=?, daily_realized_pnl=?, updated_at=?
               WHERE id=1""",
            (day, float(pnl), utcnow()),
        )

    def latest_portfolio(self):
        return self.db.query_one(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        )

    def event(self, kind: str, message: str, payload: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO system_events (ts, kind, message, payload_json) VALUES (?,?,?,?)",
            (utcnow(), kind, message, json.dumps(payload) if payload else None),
        )

    def upsert_market(self, m: dict[str, Any]) -> None:
        now = utcnow()
        self.db.execute(
            """INSERT INTO markets (
                market_id, condition_id, yes_token_id, no_token_id, question,
                description, resolution_criteria, close_time, resolution_time,
                category, event_id, correlation_group, neg_risk, tick_size,
                min_order_size, status, raw_json, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market_id) DO UPDATE SET
                condition_id=excluded.condition_id,
                yes_token_id=excluded.yes_token_id,
                no_token_id=excluded.no_token_id,
                question=excluded.question,
                description=excluded.description,
                resolution_criteria=excluded.resolution_criteria,
                close_time=excluded.close_time,
                resolution_time=excluded.resolution_time,
                category=excluded.category,
                event_id=excluded.event_id,
                correlation_group=excluded.correlation_group,
                neg_risk=excluded.neg_risk,
                tick_size=excluded.tick_size,
                min_order_size=excluded.min_order_size,
                status=excluded.status,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                m["market_id"],
                m.get("condition_id"),
                m.get("yes_token_id"),
                m.get("no_token_id"),
                m.get("question"),
                m.get("description"),
                m.get("resolution_criteria"),
                m.get("close_time"),
                m.get("resolution_time"),
                m.get("category"),
                m.get("event_id"),
                m.get("correlation_group"),
                1 if m.get("neg_risk") else 0,
                m.get("tick_size"),
                m.get("min_order_size"),
                m.get("status"),
                json.dumps(m.get("raw")) if m.get("raw") is not None else None,
                now,
            ),
        )

    def insert_snapshot(self, market_id: str, snap: dict[str, Any]) -> int:
        cur = self.db.execute(
            """INSERT INTO market_snapshots (
                market_id, ts, yes_price, no_price, midpoint, spread, volume,
                liquidity, best_bid, best_ask
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                market_id,
                snap.get("ts") or utcnow(),
                snap.get("yes_price"),
                snap.get("no_price"),
                snap.get("midpoint"),
                snap.get("spread"),
                snap.get("volume"),
                snap.get("liquidity"),
                snap.get("best_bid"),
                snap.get("best_ask"),
            ),
        )
        return int(cur.lastrowid)

    def insert_book(
        self,
        market_id: str,
        token_id: str,
        bids: list,
        asks: list,
        tick_size: str | None,
        min_order_size: str | None,
        neg_risk: bool,
        book_hash: str | None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO orderbook_snapshots (
                market_id, token_id, ts, bids_json, asks_json, tick_size,
                min_order_size, neg_risk, hash
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                market_id,
                token_id,
                utcnow(),
                json.dumps(bids),
                json.dumps(asks),
                tick_size,
                min_order_size,
                1 if neg_risk else 0,
                book_hash,
            ),
        )
        return int(cur.lastrowid)

    def insert_evidence(self, market_id: str, packet: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO research_evidence (market_id, ts, packet_json) VALUES (?,?,?)",
            (market_id, utcnow(), json.dumps(packet)),
        )
        return int(cur.lastrowid)

    def insert_prediction(self, row: dict[str, Any]) -> int:
        cur = self.db.execute(
            """INSERT INTO ai_predictions (
                market_id, ts, prompt_version, model, estimated_probability,
                confidence, confidence_score, should_abstain, estimate_json, evidence_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                row["market_id"],
                utcnow(),
                row["prompt_version"],
                row["model"],
                row.get("estimated_probability"),
                row.get("confidence"),
                row.get("confidence_score"),
                1 if row.get("should_abstain") else 0,
                json.dumps(row["estimate_json"]),
                row.get("evidence_id"),
            ),
        )
        return int(cur.lastrowid)

    def insert_decision(self, row: dict[str, Any]) -> int:
        cur = self.db.execute(
            """INSERT INTO trade_decisions (
                ts, market_id, token_id, side, approved, reject_reason, gates_json,
                grok_p, market_price, raw_edge, execution_adjusted_edge, kelly,
                size_usd, size_shares, strategy_version, prompt_version,
                snapshot_id, prediction_id, idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                row["market_id"],
                row.get("token_id"),
                row.get("side"),
                1 if row.get("approved") else 0,
                row.get("reject_reason"),
                json.dumps(row.get("gates") or {}),
                row.get("grok_p"),
                row.get("market_price"),
                row.get("raw_edge"),
                row.get("execution_adjusted_edge"),
                row.get("kelly"),
                row.get("size_usd"),
                row.get("size_shares"),
                row.get("strategy_version"),
                row.get("prompt_version"),
                row.get("snapshot_id"),
                row.get("prediction_id"),
                row.get("idempotency_key"),
            ),
        )
        return int(cur.lastrowid)

    def get_decision_by_idempotency(self, key: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM trade_decisions WHERE idempotency_key=?", (key,)
        )

    def latest_approved_decision(self, token_id: str) -> sqlite3.Row | None:
        return self.db.query_one(
            """SELECT * FROM trade_decisions
               WHERE approved=1 AND token_id=?
               ORDER BY id DESC LIMIT 1""",
            (token_id,),
        )

    def insert_order(self, row: dict[str, Any]) -> int:
        now = utcnow()
        cur = self.db.execute(
            """INSERT INTO orders (
                client_order_id, idempotency_key, broker, market_id, token_id,
                side, price, size_shares, status, decision_id, remote_order_id,
                created_at, updated_at, extra_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row.get("client_order_id"),
                row.get("idempotency_key"),
                row["broker"],
                row.get("market_id"),
                row.get("token_id"),
                row.get("side"),
                row.get("price"),
                row.get("size_shares"),
                row["status"],
                row.get("decision_id"),
                row.get("remote_order_id"),
                now,
                now,
                json.dumps(row.get("extra")) if row.get("extra") else None,
            ),
        )
        return int(cur.lastrowid)

    def update_order(self, order_id: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE orders SET {cols} WHERE id=?",
            (*fields.values(), order_id),
        )

    def get_order_by_idempotency(self, key: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM orders WHERE idempotency_key=? ORDER BY id DESC LIMIT 1",
            (key,),
        )

    def open_orders(self) -> list:
        return self.db.query(
            "SELECT * FROM orders WHERE status IN ('CREATED','SUBMITTED','OPEN','PARTIALLY_FILLED')"
        )

    def insert_fill(self, row: dict[str, Any]) -> int:
        cur = self.db.execute(
            """INSERT INTO fills (
                order_id, ts, token_id, side, shares, price, fee, slippage, is_partial
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row.get("order_id"),
                utcnow(),
                row.get("token_id"),
                row.get("side"),
                row["shares"],
                row["price"],
                row.get("fee") or 0,
                row.get("slippage") or 0,
                1 if row.get("is_partial") else 0,
            ),
        )
        return int(cur.lastrowid)

    def upsert_position(self, row: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT INTO positions (
                token_id, market_id, shares, avg_price, realized_pnl, category,
                correlation_group, updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(token_id) DO UPDATE SET
                shares=excluded.shares,
                avg_price=excluded.avg_price,
                realized_pnl=excluded.realized_pnl,
                category=excluded.category,
                correlation_group=excluded.correlation_group,
                updated_at=excluded.updated_at
            """,
            (
                row["token_id"],
                row.get("market_id"),
                row["shares"],
                row["avg_price"],
                row.get("realized_pnl") or 0,
                row.get("category"),
                row.get("correlation_group"),
                utcnow(),
            ),
        )

    def positions(self) -> list:
        return self.db.query("SELECT * FROM positions WHERE shares > 0")

    def insert_portfolio(self, row: dict[str, Any]) -> None:
        self.db.execute(
            """INSERT INTO portfolio_snapshots (
                ts, cash, reserved_cash, equity, exposure, realized_pnl,
                unrealized_pnl, extra_json
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                row["cash"],
                row["reserved_cash"],
                row["equity"],
                row["exposure"],
                row["realized_pnl"],
                row["unrealized_pnl"],
                json.dumps(row.get("extra")) if row.get("extra") else None,
            ),
        )

    def insert_activation(self, row: dict[str, Any]) -> int:
        cur = self.db.execute(
            """INSERT INTO activation_records (
                ts, operator_confirmation, paper_duration_seconds, git_commit,
                config_hash, strategy_version, prompt_version, risk_config_json,
                report_hash
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(),
                row["operator_confirmation"],
                row.get("paper_duration_seconds"),
                row.get("git_commit"),
                row.get("config_hash"),
                row.get("strategy_version"),
                row.get("prompt_version"),
                json.dumps(row.get("risk_config")),
                row.get("report_hash"),
            ),
        )
        return int(cur.lastrowid)

    def latest_activation(self) -> Any:
        return self.db.query_one(
            "SELECT * FROM activation_records ORDER BY id DESC LIMIT 1"
        )

    def counts(self) -> dict[str, int]:
        def n(table: str) -> int:
            row = self.db.query_one(f"SELECT COUNT(*) AS c FROM {table}")
            return int(row["c"]) if row else 0

        return {
            "markets": n("markets"),
            "predictions": n("ai_predictions"),
            "decisions": n("trade_decisions"),
            "orders": n("orders"),
            "fills": n("fills"),
            "events": n("system_events"),
        }

    def predictions(self) -> list:
        return self.db.query("SELECT * FROM ai_predictions ORDER BY id")

    def decisions(self, limit: int = 50) -> list:
        return self.db.query(
            "SELECT * FROM trade_decisions ORDER BY id DESC LIMIT ?", (limit,)
        )

    def orders(self, limit: int = 50) -> list:
        return self.db.query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    def upsert_prompt(self, version: str, body: str) -> None:
        self.db.execute(
            """INSERT INTO prompt_versions (version, body, created_at) VALUES (?,?,?)
               ON CONFLICT(version) DO UPDATE SET body=excluded.body""",
            (version, body, utcnow()),
        )

    def event_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT kind, COUNT(*) AS c FROM system_events GROUP BY kind"
        )
        return {r["kind"]: int(r["c"]) for r in rows}

    def insert_resolved(self, market_id: str, outcome: str, prediction_id: int | None, brier: float | None) -> None:
        self.db.execute(
            """INSERT INTO resolved_markets (market_id, outcome, resolved_at, prediction_id, brier)
               VALUES (?,?,?,?,?)
               ON CONFLICT(market_id) DO UPDATE SET outcome=excluded.outcome, brier=excluded.brier""",
            (market_id, outcome, utcnow(), prediction_id, brier),
        )

    def resolved(self) -> list:
        return self.db.query("SELECT * FROM resolved_markets")
