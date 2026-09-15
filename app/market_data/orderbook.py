"""Orderbook normalize, walk, VWAP, slippage. Never assume midpoint fills."""

from __future__ import annotations

from dataclasses import dataclass

from app.market_data.models import BookLevel, OrderBook


@dataclass
class FillEstimate:
    filled_shares: float
    vwap: float
    notional: float
    unfilled_shares: float
    slippage_pct: float
    best_price: float
    levels_taken: int
    fully_filled: bool


def normalize_levels(levels: list[BookLevel], *, asks: bool) -> list[BookLevel]:
    cleaned = [lvl for lvl in levels if lvl.size > 0 and 0 < lvl.price < 1]
    if asks:
        return sorted(cleaned, key=lambda x: x.price)
    return sorted(cleaned, key=lambda x: x.price, reverse=True)


def book_from_raw(
    token_id: str,
    bids: list[dict],
    asks: list[dict],
    *,
    market_id: str = "",
    tick_size: float = 0.01,
    min_order_size: float = 1.0,
    neg_risk: bool = False,
    book_hash: str | None = None,
) -> OrderBook:
    def lvl(x: dict) -> BookLevel:
        return BookLevel(price=float(x["price"]), size=float(x["size"]))

    return OrderBook(
        token_id=token_id,
        market_id=market_id,
        bids=normalize_levels([lvl(x) for x in bids], asks=False),
        asks=normalize_levels([lvl(x) for x in asks], asks=True),
        tick_size=tick_size,
        min_order_size=min_order_size,
        neg_risk=neg_risk,
        hash=book_hash,
    )


def walk_book(book: OrderBook, side: str, shares: float) -> FillEstimate:
    """BUY walks asks; SELL walks bids. Conservative: leftover is unfilled."""
    if shares <= 0:
        return FillEstimate(0, 0, 0, 0, 0, 0, 0, False)
    levels = book.asks if side.upper() == "BUY" else book.bids
    if not levels:
        return FillEstimate(0, 0, 0, shares, 1.0, 0, 0, False)
    best = levels[0].price
    remaining = shares
    cost = 0.0
    taken = 0
    filled = 0.0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl.size)
        cost += take * lvl.price
        remaining -= take
        filled += take
        taken += 1
    if filled <= 0:
        return FillEstimate(0, 0, 0, shares, 1.0, best, 0, False)
    vwap = cost / filled
    if side.upper() == "BUY":
        slip = max(0.0, (vwap - best) / best) if best else 1.0
    else:
        slip = max(0.0, (best - vwap) / best) if best else 1.0
    return FillEstimate(
        filled_shares=filled,
        vwap=vwap,
        notional=cost,
        unfilled_shares=remaining,
        slippage_pct=slip,
        best_price=best,
        levels_taken=taken,
        fully_filled=remaining <= 1e-9,
    )


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    n = round(price / tick)
    return max(tick, min(1.0 - tick, n * tick))
