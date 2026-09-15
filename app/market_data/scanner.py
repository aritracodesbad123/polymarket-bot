from __future__ import annotations

from app.config import Settings
from app.market_data.models import Market, OrderBook, utcnow
from app.market_data.client import PolymarketClient


def filter_market(m: Market, settings: Settings, now=None) -> str | None:
    """Return reject reason or None if the market may proceed."""
    now = now or utcnow()
    if m.closed or m.status == "closed":
        return "closed"
    if m.paused or m.status == "paused":
        return "paused"
    if not m.active:
        return "inactive"
    if not m.question.strip():
        return "malformed"
    if not m.yes_token_id or not m.no_token_id:
        return "malformed_tokens"
    if not (m.resolution_criteria or m.description):
        return "unclear_resolution"
    if m.market_id in settings.market_blacklist:
        return "blacklisted_market"
    if m.category.lower() in {c.lower() for c in settings.category_blacklist}:
        return "blacklisted_category"
    if settings.universe_tag:
        tag = settings.universe_tag.lower()
        cat = (m.category or "").lower()
        q = (m.question or "").lower()
        # Prefer stamped tag/category; also accept obvious crypto keywords as belt-and-suspenders.
        keywords = (tag, "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto")
        if tag not in cat and not any(k in cat or k in q for k in keywords if k):
            return "outside_universe"
    if m.liquidity < settings.min_liquidity:
        return "insufficient_liquidity"
    if m.volume < settings.min_volume:
        return "insufficient_volume"
    hours = m.hours_to_resolution(now)
    if hours is None:
        return "missing_resolution_time"
    if hours < settings.min_time_to_resolution_hours:
        return "too_close_to_resolution"
    if hours > settings.max_time_to_resolution_hours:
        return "too_far_from_resolution"
    return None


def filter_book(book: OrderBook, settings: Settings, now=None) -> str | None:
    if book.age_seconds(now) > settings.max_data_age_seconds:
        return "stale_data"
    if book.spread is None or book.spread > settings.max_spread:
        return "spread_too_wide"
    if not book.asks or not book.bids:
        return "empty_book"
    return None


class MarketScanner:
    def __init__(self, client: PolymarketClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def scan(self) -> list[tuple[Market, str | None]]:
        """Build the cycle universe: oversample, rank, prefer markets that clear floors.

        Floors stay unchanged — we just stop wasting the top-N slot on markets that
        cannot pass MIN_LIQUIDITY / MIN_VOLUME / resolution window.
        """
        cycle_n = self.settings.max_markets_per_cycle
        pool_n = max(cycle_n * 5, 250)
        if self.settings.universe_tag:
            markets = await self.client.list_markets_for_tag(
                self.settings.universe_tag, limit=pool_n
            )
        else:
            markets = await self.client.list_markets(closed=False, limit=pool_n)
        scored = [(m, filter_market(m, self.settings)) for m in markets]
        passers = [(m, r) for m, r in scored if r is None]
        others = [(m, r) for m, r in scored if r is not None]
        # Passers first (already liquidity/volume ranked from client), then the rest
        ordered = passers + others
        return ordered[:cycle_n]

    async def candidates(self) -> list[Market]:
        rows = await self.scan()
        return [m for m, reason in rows if reason is None]
