"""Diagnose open holdings without LLM — books + entry thesis only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.market_data.models import Market, OrderBook


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
    # best_bid | last_trade | best_ask | resolution | none
    mark_source: str = "none"
    detail: str = ""


def _holding_mark(book: OrderBook) -> tuple[float | None, str]:
    """Price for the stop, and the field it came from.

    Best bid wins, including 0 (the contract is worthless). A missing bid used
    to become "no mark" and a healthy hold; the console still prints the ask
    or the last trade in that book. Those are real stop marks.
    """
    if book.best_bid is not None:
        return float(book.best_bid), "best_bid"
    if book.last_trade_price is not None:
        return float(book.last_trade_price), "last_trade"
    if book.best_ask is not None:
        return float(book.best_ask), "best_ask"
    return None, "none"


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
    mark, mark_source = _holding_mark(book)
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
            mark_source=mark_source,
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
            mark_source=mark_source,
            detail=(
                f"unreal_pct={unreal_pct:.4f} mark_source={mark_source} "
                f"mark={mark:.6f}"
            ),
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
            mark_source=mark_source,
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
            mark_source=mark_source,
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
        mark_source=mark_source,
    )


# Outcome prices inside this band of 0 or 1 are a resolution, not a live quote.
# Covers the published 0/1 pins and the 0.001 dust print on a settled book.
_RESOLUTION_PIN = 0.02


def _market_is_resolved(market: Market) -> bool:
    if market.closed or (market.status or "").lower() == "closed":
        return True
    raw = market.raw if isinstance(market.raw, dict) else {}
    status = str(
        raw.get("umaResolutionStatus") or raw.get("uma_resolution_status") or ""
    ).lower()
    return status in {"resolved", "settled"}


def _token_outcome_price(market: Market, token_id: str) -> float | None:
    if token_id and token_id == (market.yes_token_id or "") and market.yes_price is not None:
        return float(market.yes_price)
    if token_id and token_id == (market.no_token_id or "") and market.no_price is not None:
        return float(market.no_price)
    raw = market.raw if isinstance(market.raw, dict) else {}
    tokens = raw.get("tokens")
    if not isinstance(tokens, list):
        return None
    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        tid = str(tok.get("token_id") or tok.get("tokenId") or "")
        if tid != token_id:
            continue
        winner = tok.get("winner")
        if winner is True:
            return 1.0
        if winner is False:
            return 0.0
        px = tok.get("price")
        if px is None:
            return None
        try:
            return float(px)
        except (TypeError, ValueError):
            return None
    return None


def resolution_price(market: Market, token_id: str) -> float | None:
    """Settlement price for this token, or None if the market is still live.

    Closed markets whose outcome is still mid-book are not settled here.
    """
    if not _market_is_resolved(market):
        return None
    px = _token_outcome_price(market, token_id)
    if px is None:
        return None
    if px <= _RESOLUTION_PIN or px >= 1.0 - _RESOLUTION_PIN:
        return float(px)
    return None


def market_outcome_label(market: Market, token_id: str, price: float) -> str:
    """YES/NO label for the market, from the token we held and its settlement."""
    won = price >= 0.5
    if token_id and token_id == (market.no_token_id or ""):
        return "NO" if won else "YES"
    return "YES" if won else "NO"


def thesis_from_decision(row: Any | None) -> tuple[float | None, str | None]:
    """Return (entry_p, ts) from a trade_decisions row."""
    if row is None:
        return None, None
    keys = row.keys() if hasattr(row, "keys") else ()
    p = float(row["grok_p"]) if "grok_p" in keys and row["grok_p"] is not None else None
    ts = row["ts"] if "ts" in keys else None
    return p, ts
