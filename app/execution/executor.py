"""Idempotent submit. Reconcile before retry. Never duplicate."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone

from app.broker.live import LiveBroker, LiveLockedError
from app.broker.models import OrderRequest, OrderStatus
from app.broker.paper import PaperBroker
from app.config import Settings
from app.market_data.client import PolymarketClient
from app.market_data.scanner import filter_book
from app.risk.caps import (
    add_breaches_cap,
    position_cost_from_rows,
    position_notional_cap,
    total_exposure_cap,
)
from app.storage.repositories import Repositories
from app.strategy.evaluator import Decision


WINDOW_SECONDS = 6 * 60 * 60


def idempotency_key(
    market_id: str,
    token_id: str,
    side: str,
    strategy_version: str,
    ts: float | None = None,
    *,
    kind: str = "entry",
) -> str:
    window = int((ts or time.time()) // WINDOW_SECONDS)
    raw = f"{market_id}|{token_id}|{side}|{strategy_version}|{window}|{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Executor:
    def __init__(
        self,
        settings: Settings,
        repo: Repositories,
        paper: PaperBroker,
        live: LiveBroker,
        data: PolymarketClient,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.paper = paper
        self.live = live
        self.data = data

    def _broker(self):
        st = self.repo.state()
        if st.trading_mode == "live" and self.live.authorize_now().allowed:
            return self.live
        return self.paper

    async def execute(
        self, decision: Decision, decision_id: int, *, kind: str = "entry"
    ) -> str | None:
        if not decision.approved or not decision.token_id:
            return "not_approved"
        key = idempotency_key(
            decision.market_id,
            decision.token_id,
            decision.side or "BUY",
            self.settings.strategy_version,
            kind=kind,
        )
        existing = self.repo.get_order_by_idempotency(key)
        if existing is not None:
            return "duplicate_order"
        if self.repo.get_decision_by_idempotency(key) and existing:
            return "duplicate_order"

        book = await self.data.get_order_book(decision.token_id, decision.market_id)
        stale = filter_book(book, self.settings)
        if stale:
            return stale

        cid = str(uuid.uuid4())
        req = OrderRequest(
            client_order_id=cid,
            idempotency_key=key,
            market_id=decision.market_id,
            token_id=decision.token_id,
            side=decision.side or "BUY",
            price=float(decision.limit_price or decision.market_price or 0),
            size_shares=decision.size_shares,
            decision_id=decision_id,
        )
        broker = self._broker()
        if req.side.upper() == "BUY":
            notional = req.price * req.size_shares
            bankroll = self.settings.paper_starting_bankroll
            pos_cap = position_notional_cap(self.settings, bankroll)
            # Cost basis of this token, plus resting BUY reserve. Unknown
            # inventory fails closed — do not add onto a book we cannot see.
            committed = await self._token_commitment(broker, req.token_id)
            if committed is None or add_breaches_cap(committed, notional, pos_cap):
                return "position_usd_cap"
            exposure_fn = getattr(broker, "exposure", None)
            if callable(exposure_fn):
                exp_cap = total_exposure_cap(self.settings, bankroll)
                if add_breaches_cap(float(exposure_fn()), notional, exp_cap):
                    return "exposure_usd_cap"
        oid = self.repo.insert_order(
            {
                "client_order_id": cid,
                "idempotency_key": key,
                "broker": broker.name,
                "market_id": decision.market_id,
                "token_id": decision.token_id,
                "side": req.side,
                "price": req.price,
                "size_shares": req.size_shares,
                "status": OrderStatus.SUBMITTED.value,
                "decision_id": decision_id,
            }
        )
        try:
            rec = await broker.submit(req, book, decision)
        except TimeoutError:
            mismatch = await broker.reconcile()
            if mismatch:
                return f"timeout_reconcile:{mismatch}"
            existing = self.repo.get_order_by_idempotency(key)
            if existing:
                return "timeout_already_exists"
            return "timeout_unknown_fail_closed"
        except LiveLockedError as exc:
            self.repo.update_order(oid, status=OrderStatus.REJECTED.value, extra_json=str(exc.reasons))
            return "live_locked"
        self.repo.update_order(
            oid,
            status=rec.status.value,
            remote_order_id=rec.remote_order_id,
        )
        if rec.filled_shares > 0:
            self.repo.insert_fill(
                {
                    "order_id": oid,
                    "token_id": decision.token_id,
                    "side": req.side,
                    "shares": rec.filled_shares,
                    "price": rec.avg_fill_price,
                    "is_partial": rec.status == OrderStatus.PARTIALLY_FILLED,
                    "slippage": (decision.fill.slippage_pct if decision.fill else 0),
                }
            )
            kind = "PAPER_FILL" if broker.name == "paper" else "LIVE_FILL"
            self.repo.event(kind, f"{decision.market_id} {rec.filled_shares}@{rec.avg_fill_price}")
        kind = "PAPER_ORDER" if broker.name == "paper" else "LIVE_ORDER"
        self.repo.event(kind, rec.status.value, {"client_order_id": cid})
        return None

    async def _token_commitment(self, broker, token_id: str) -> float | None:
        """Open cost basis plus resting BUY notional for ``token_id``.

        None when inventory cannot be read (fail closed at the caller).
        """
        try:
            rows = await broker.positions()
        except Exception:
            return None
        cost = position_cost_from_rows(rows, token_id)
        reserve = getattr(broker, "resting_buy_usd", None)
        if not callable(reserve):
            return cost
        try:
            return cost + float(reserve(token_id))
        except Exception:
            return None
