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
    if settings.universe_tags:
        cat = (m.category or "").lower()
        q = (m.question or "").lower()
        blob = f"{cat} {q}"
        tags = {t.lower() for t in settings.universe_tags}
        # Belt-and-suspenders keywords for crypto + FX/metals that may not stamp category.
        keywords = {
            "crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
            "forex", "fx", "eurusd", "gbpusd", "usdjpy", "xauusd", "xagusd",
            "xau", "xag", "gold", "silver", "dxy", "usd",
        }
        # Keep keyword set relevant to selected tags.
        if "crypto" not in tags:
            keywords -= {"crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "sol"}
        if not tags.intersection({"forex", "fx"}):
            keywords -= {"forex", "fx", "eurusd", "gbpusd", "usdjpy", "xauusd", "xagusd", "xau", "xag", "gold", "silver", "dxy", "usd"}
        in_tag = any(t in cat for t in tags)
        in_kw = any(k in blob for k in keywords)
        if not in_tag and not in_kw:
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


# Pre-AI reject. Same string in trade_decisions.reject_reason and system_events.
MID_OUTSIDE_BAND = "mid_outside_band"


def filter_tradeable_mid(mid: float | None, settings: Settings) -> str | None:
    """Skip the model when the book mid is outside the tradeable band.

    Edges are tradeable: ``min_tradeable_mid <= mid <= max_tradeable_mid``
    (defaults 0.10 and 0.90). A missing mid is not tradeable.
    """
    lo = settings.min_tradeable_mid
    hi = settings.max_tradeable_mid
    if mid is None or mid < lo or mid > hi:
        return MID_OUTSIDE_BAND
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
        if self.settings.universe_tags:
            markets = await self.client.list_markets_for_tags(
                self.settings.universe_tags, limit=pool_n
            )
        else:
            markets = await self.client.list_markets(closed=False, limit=pool_n)
        scored = [(m, filter_market(m, self.settings)) for m in markets]
        passers = [(m, r) for m, r in scored if r is None]
        others = [(m, r) for m, r in scored if r is not None]
        ordered = passers + others
        tags = [t.lower() for t in (self.settings.universe_tags or ())]
        if len(tags) <= 1 or cycle_n < len(tags):
            return ordered[:cycle_n]
        # Final cut must also reserve slots — pool diversify alone still puts one
        # tag's block first, so [:cycle_n] was crypto-only.
        per = max(cycle_n // len(tags), 1)
        picked: list[tuple] = []
        seen: set[str] = set()

        def _tag_of(m) -> str:
            cat = (m.category or "").lower()
            for t in tags:
                if t == cat or t in cat:
                    return t
            blob = f"{cat} {(m.question or '').lower()}"
            for t in tags:
                if t in blob:
                    return t
            return cat or "other"

        for t in tags:
            n = 0
            for m, r in ordered:
                if m.market_id in seen:
                    continue
                if _tag_of(m) != t:
                    continue
                picked.append((m, r))
                seen.add(m.market_id)
                n += 1
                if n >= per:
                    break
        for m, r in ordered:
            if len(picked) >= cycle_n:
                break
            if m.market_id not in seen:
                picked.append((m, r))
                seen.add(m.market_id)
        return picked[:cycle_n]

    async def candidates(self) -> list[Market]:
        rows = await self.scan()
        return [m for m, reason in rows if reason is None]
