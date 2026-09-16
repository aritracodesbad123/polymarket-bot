"""Diagnose open holdings without LLM — books + entry thesis only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.market_data.models import OrderBook


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass
class HoldingVerdict:
    token_id: str
    market_id: str
    reason: str  # ok | thesis_broken | stop_loss | time_decay
    entry_p: float | None
    mark: float | None
    edge_now: float | None
    unreal_pct: float | None
    hours_held: float | None
    shares: float
    avg_price: float
    detail: str = ""


def diagnose_holding(
    *,
    shares: float,
    avg_price: float,
    token_id: str,
    market_id: str,
    book: OrderBook,
    entry_p: float | None,
    entry_ts: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> HoldingVerdict:
    now = now or datetime.now(timezone.utc)
    mark = book.best_bid if book.best_bid is not None else book.midpoint
    if mark is None:
        return HoldingVerdict(
            token_id=token_id,
            market_id=market_id,
            reason="ok",
            entry_p=entry_p,
            mark=None,
            edge_now=None,
            unreal_pct=None,
            hours_held=None,
            shares=shares,
            avg_price=avg_price,
            detail="no_mark",
        )
    cost = shares * avg_price
    unreal = shares * (mark - avg_price)
    unreal_pct = (unreal / cost) if cost > 1e-12 else 0.0
    edge_now = (entry_p - mark) if entry_p is not None else None
    held = None
    ts = _parse_ts(entry_ts)
    if ts is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        held = (now - ts).total_seconds() / 3600.0

    if unreal_pct <= -settings.holding_stop_pct:
        return HoldingVerdict(
            token_id=token_id,
            market_id=market_id,
            reason="stop_loss",
            entry_p=entry_p,
            mark=mark,
            edge_now=edge_now,
            unreal_pct=unreal_pct,
            hours_held=held,
            shares=shares,
            avg_price=avg_price,
            detail=f"unreal_pct={unreal_pct:.4f}",
        )
    if edge_now is not None and edge_now < -settings.holding_thesis_edge:
        return HoldingVerdict(
            token_id=token_id,
            market_id=market_id,
            reason="thesis_broken",
            entry_p=entry_p,
            mark=mark,
            edge_now=edge_now,
            unreal_pct=unreal_pct,
            hours_held=held,
            shares=shares,
            avg_price=avg_price,
            detail=f"edge_now={edge_now:.4f}",
        )
    if (
        held is not None
        and held > settings.holding_max_hours
        and unreal < 0
    ):
        return HoldingVerdict(
            token_id=token_id,
            market_id=market_id,
            reason="time_decay",
            entry_p=entry_p,
            mark=mark,
            edge_now=edge_now,
            unreal_pct=unreal_pct,
            hours_held=held,
            shares=shares,
            avg_price=avg_price,
            detail=f"hours={held:.1f}",
        )
    return HoldingVerdict(
        token_id=token_id,
        market_id=market_id,
        reason="ok",
        entry_p=entry_p,
        mark=mark,
        edge_now=edge_now,
        unreal_pct=unreal_pct,
        hours_held=held,
        shares=shares,
        avg_price=avg_price,
    )


def thesis_from_decision(row: Any | None) -> tuple[float | None, str | None]:
    """Return (entry_p, ts) from a trade_decisions row."""
    if row is None:
        return None, None
    keys = row.keys() if hasattr(row, "keys") else ()
    p = float(row["grok_p"]) if "grok_p" in keys and row["grok_p"] is not None else None
    ts = row["ts"] if "ts" in keys else None
    return p, ts
