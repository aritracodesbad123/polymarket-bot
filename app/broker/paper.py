"""Simulated execution against real books. Never hits CLOB."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.broker.models import OrderRecord, OrderRequest, OrderStatus
from app.market_data.models import OrderBook
from app.market_data.orderbook import walk_book
from app.strategy.evaluator import Decision, fee_per_share


@dataclass
class PaperPosition:
    token_id: str
    market_id: str
    shares: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    category: str = ""
    correlation_group: str = ""


@dataclass
class Resting:
    req: OrderRequest
    remaining: float
    created_at: float
    filled: float = 0.0
    cost: float = 0.0
    status: OrderStatus = OrderStatus.OPEN


class PaperBroker:
    name = "paper"

    def __init__(self, cash: float, latency_ms: int = 150) -> None:
        self.cash = cash
        self.reserved = 0.0
        self.latency_s = latency_ms / 1000.0
        self._positions: dict[str, PaperPosition] = {}
        self.resting: dict[str, Resting] = {}
        self.records: dict[str, OrderRecord] = {}
        self.fees_paid = 0.0
        self.realized_pnl = 0.0
        self._ok = True

    def operational(self) -> bool:
        return self._ok

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        inv = 0.0
        for p in self._positions.values():
            px = marks.get(p.token_id, p.avg_price)
            inv += p.shares * px
        return self.cash + self.reserved + inv

    def exposure(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        tot = 0.0
        for p in self._positions.values():
            px = marks.get(p.token_id, p.avg_price)
            tot += p.shares * px
        return tot + self.reserved

    async def submit(self, req: OrderRequest, book: OrderBook, decision: Decision) -> OrderRecord:
        await asyncio.sleep(self.latency_s)
        if req.side.upper() != "BUY":
            rec = OrderRecord(
                client_order_id=req.client_order_id,
                status=OrderStatus.REJECTED,
                message="paper_v1_buy_only",
            )
            self.records[req.client_order_id] = rec
            return rec
        notional = req.price * req.size_shares
        if self.cash < notional:
            rec = OrderRecord(
                client_order_id=req.client_order_id,
                status=OrderStatus.REJECTED,
                message="insufficient_cash",
            )
            self.records[req.client_order_id] = rec
            return rec
        fill = walk_book(book, "BUY", req.size_shares)
        filled = min(fill.filled_shares, req.size_shares)
        # only take size at or below limit
        if fill.vwap > req.price + 1e-9 and filled > 0:
            # conservative: skip aggressive walk beyond limit
            filled = 0.0
            for lvl in book.asks:
                if lvl.price > req.price + 1e-12:
                    break
                take = min(req.size_shares - filled, lvl.size)
                filled += take
            if filled <= 0:
                self.cash -= notional
                self.reserved += notional
                rest = Resting(req=req, remaining=req.size_shares, created_at=time.time())
                self.resting[req.client_order_id] = rest
                rec = OrderRecord(
                    client_order_id=req.client_order_id,
                    status=OrderStatus.OPEN,
                    remaining=req.size_shares,
                    remote_order_id=f"paper-{req.client_order_id}",
                )
                self.records[req.client_order_id] = rec
                return rec
        if filled <= 0:
            self.cash -= notional
            self.reserved += notional
            rest = Resting(req=req, remaining=req.size_shares, created_at=time.time())
            self.resting[req.client_order_id] = rest
            rec = OrderRecord(
                client_order_id=req.client_order_id,
                status=OrderStatus.OPEN,
                remaining=req.size_shares,
                remote_order_id=f"paper-{req.client_order_id}",
            )
            self.records[req.client_order_id] = rec
            return rec

        cost = filled * (fill.vwap if fill.fully_filled or filled == fill.filled_shares else min(fill.vwap, req.price))
        # recompute cost walking only <= limit
        cost, filled = self._fill_at_or_under(book, req.size_shares, req.price)
        fee = fee_per_share(cost / filled if filled else req.price, decision.category) * filled
        spend = cost + fee
        unfilled = req.size_shares - filled
        self.cash -= spend
        if unfilled > 0:
            reserve = unfilled * req.price
            if self.cash >= reserve:
                self.cash -= reserve
                self.reserved += reserve
            self.resting[req.client_order_id] = Resting(
                req=req,
                remaining=unfilled,
                created_at=time.time(),
                filled=filled,
                cost=cost,
                status=OrderStatus.PARTIALLY_FILLED,
            )
        self._apply_buy(req, filled, cost / filled if filled else req.price, decision)
        self.fees_paid += fee
        status = OrderStatus.FILLED if unfilled <= 1e-9 else OrderStatus.PARTIALLY_FILLED
        rec = OrderRecord(
            client_order_id=req.client_order_id,
            status=status,
            remote_order_id=f"paper-{req.client_order_id}",
            filled_shares=filled,
            avg_fill_price=cost / filled if filled else 0.0,
            remaining=unfilled,
        )
        self.records[req.client_order_id] = rec
        return rec

    def _fill_at_or_under(self, book: OrderBook, shares: float, limit: float) -> tuple[float, float]:
        remaining = shares
        cost = 0.0
        filled = 0.0
        for lvl in book.asks:
            if lvl.price > limit + 1e-12:
                break
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            remaining -= take
            filled += take
            if remaining <= 0:
                break
        return cost, filled

    def _apply_buy(self, req: OrderRequest, shares: float, price: float, decision: Decision) -> None:
        pos = self._positions.get(req.token_id)
        if pos is None:
            pos = PaperPosition(
                token_id=req.token_id,
                market_id=req.market_id,
                category=decision.category,
                correlation_group=decision.correlation_group,
            )
            self._positions[req.token_id] = pos
        total = pos.shares + shares
        pos.avg_price = (pos.avg_price * pos.shares + price * shares) / total if total else price
        pos.shares = total

    def on_book(self, book: OrderBook) -> list[OrderRecord]:
        """Match resting GTC buys against a fresh book."""
        out: list[OrderRecord] = []
        for cid, rest in list(self.resting.items()):
            if rest.req.token_id != book.token_id:
                continue
            cost, filled = self._fill_at_or_under(book, rest.remaining, rest.req.price)
            if filled <= 0:
                continue
            reserve_release = filled * rest.req.price
            self.reserved = max(0.0, self.reserved - reserve_release)
            leftover_reserve = reserve_release - cost
            if leftover_reserve > 0:
                self.cash += leftover_reserve
            decision = Decision(
                approved=True,
                reject_reason=None,
                gates=[],
                market_id=rest.req.market_id,
                category="",
                correlation_group="",
            )
            self._apply_buy(rest.req, filled, cost / filled, decision)
            rest.remaining -= filled
            rest.filled += filled
            rest.cost += cost
            rec = self.records.get(cid) or OrderRecord(client_order_id=cid, status=OrderStatus.OPEN)
            rec.filled_shares = rest.filled
            rec.avg_fill_price = rest.cost / rest.filled if rest.filled else 0
            rec.remaining = rest.remaining
            if rest.remaining <= 1e-9:
                rec.status = OrderStatus.FILLED
                leftover = rest.remaining * rest.req.price
                if leftover:
                    self.reserved = max(0.0, self.reserved - leftover)
                    self.cash += leftover
                del self.resting[cid]
            else:
                rec.status = OrderStatus.PARTIALLY_FILLED
            self.records[cid] = rec
            out.append(rec)
        return out

    async def cancel(self, client_order_id: str) -> OrderRecord:
        rest = self.resting.pop(client_order_id, None)
        rec = self.records.get(client_order_id) or OrderRecord(
            client_order_id=client_order_id, status=OrderStatus.CANCELLED
        )
        if rest:
            refund = rest.remaining * rest.req.price
            self.reserved = max(0.0, self.reserved - refund)
            self.cash += refund
        rec.status = OrderStatus.CANCELLED
        rec.remaining = 0
        self.records[client_order_id] = rec
        return rec

    async def get_order(self, client_order_id: str) -> OrderRecord | None:
        return self.records.get(client_order_id)

    async def open_orders(self) -> list[OrderRecord]:
        return [
            self.records[k]
            for k in self.resting
            if k in self.records
        ]

    async def positions(self) -> list[dict]:
        return [
            {
                "token_id": p.token_id,
                "market_id": p.market_id,
                "shares": p.shares,
                "avg_price": p.avg_price,
                "category": p.category,
                "correlation_group": p.correlation_group,
                "realized_pnl": p.realized_pnl,
            }
            for p in self._positions.values()
            if p.shares > 0
        ]

    async def balances(self) -> dict:
        return {"cash": self.cash, "reserved": self.reserved, "currency": "pUSD-paper"}

    async def reconcile(self) -> str | None:
        if self.cash < -1e-6 or self.reserved < -1e-6:
            return "negative_cash"
        return None
