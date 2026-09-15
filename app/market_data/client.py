"""Read-only Polymarket market data. Never places orders. Never loads keys."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.market_data.models import Market, OrderBook, utcnow
from app.market_data.orderbook import book_from_raw


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default

def _num(*values: Any, default: float = 0.0) -> float:
    """Coerce first usable numeric (handles string Gamma fields)."""
    for v in values:
        if v is None or v is False:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if not s:
                continue
            try:
                return float(s)
            except ValueError:
                continue
    return default


def rank_markets_for_universe(markets: list[Market]) -> list[Market]:
    """Liquidity-first universe ranking before the top-N cut."""
    return sorted(markets, key=lambda m: (m.liquidity, m.volume), reverse=True)


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _tokens(market: Any) -> tuple[str | None, str | None]:
    outcomes = _get(market, "outcomes")
    yes = _get(outcomes, "yes") if outcomes is not None else None
    no = _get(outcomes, "no") if outcomes is not None else None
    yes_id = _get(yes, "token_id", "tokenId")
    no_id = _get(no, "token_id", "tokenId")
    if yes_id and no_id:
        return str(yes_id), str(no_id)
    raw = _get(market, "clob_token_ids", "clobTokenIds", "clobTokenIDs")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, (list, tuple)) and raw:
        yes_id = str(raw[0])
        no_id = str(raw[1]) if len(raw) > 1 else None
        return yes_id, no_id
    return None, None


def market_from_sdk(obj: Any) -> Market | None:
    mid = _get(obj, "id", "market_id", "slug")
    if mid is None:
        return None
    yes_id, no_id = _tokens(obj)
    question = str(_get(obj, "question", "title", default="") or "")
    desc = str(_get(obj, "description", default="") or "")
    criteria = str(
        _get(obj, "resolution_source", "resolutionSource", "description", default="")
        or desc
    )
    event = _get(obj, "event", "events")
    event_id = None
    category = ""
    if isinstance(event, (list, tuple)) and event:
        event_id = str(_get(event[0], "id", default="") or "") or None
        category = str(_get(event[0], "category", "ticker", default="") or "")
    elif event is not None:
        event_id = str(_get(event, "id", default="") or "") or None
        category = str(_get(event, "category", "ticker", default="") or "")
    tags = _get(obj, "tags") or []
    if not category and tags:
        t0 = tags[0]
        category = str(_get(t0, "label", "slug", default=t0) or "")
    closed = bool(_get(obj, "closed", default=False))
    active = bool(_get(obj, "active", default=not closed))
    paused = bool(_get(obj, "archived", "paused", default=False))
    vol = _num(
        _get(obj, "volume_24hr", "volume24hr", "volumeNum", "volume_num", "volume"),
        default=0.0,
    )
    # Prefer explicit numeric / CLOB depth fields; plain "liquidity" is often a string or stale.
    liq = _num(
        _get(
            obj,
            "liquidityNum",
            "liquidity_num",
            "liquidityClob",
            "liquidity_clob",
            "liquidityAmm",
            "liquidity_amm",
            "liquidity",
        ),
        default=0.0,
    )

    tick = float(_get(obj, "minimum_tick_size", "order_price_min_tick_size", "tick_size", default=0.01) or 0.01)
    mos = float(_get(obj, "minimum_order_size", "min_order_size", default=1) or 1)
    neg = bool(_get(obj, "neg_risk", "negRisk", default=False))
    yes_px = _get(obj, "outcome_prices", "outcomePrices", "best_ask")
    yes_price = None
    no_price = None
    if isinstance(yes_px, str):
        try:
            yes_px = json.loads(yes_px)
        except json.JSONDecodeError:
            yes_px = None
    if isinstance(yes_px, (list, tuple)) and yes_px:
        try:
            yes_price = float(yes_px[0])
            no_price = float(yes_px[1]) if len(yes_px) > 1 else None
        except (TypeError, ValueError):
            pass
    close = _parse_dt(_get(obj, "end_date", "endDate", "close_time", "end_date_iso"))
    status = "closed" if closed else ("paused" if paused or not active else "active")
    corr = event_id or str(mid)
    raw = obj if isinstance(obj, dict) else {}
    return Market(
        market_id=str(mid),
        condition_id=str(_get(obj, "condition_id", "conditionId", default="") or "") or None,
        yes_token_id=yes_id,
        no_token_id=no_id,
        question=question,
        description=desc,
        resolution_criteria=criteria,
        close_time=close,
        resolution_time=close,
        category=category or "other",
        event_id=event_id,
        correlation_group=corr,
        neg_risk=neg,
        tick_size=tick,
        min_order_size=mos,
        status=status,
        volume=vol,
        liquidity=liq,
        yes_price=yes_price,
        no_price=no_price,
        active=active,
        closed=closed,
        paused=paused,
        raw=raw,
    )


def _book_levels(obj: Any, key: str) -> list[dict]:
    raw = _get(obj, key, default=[]) or []
    out = []
    for x in raw:
        if isinstance(x, dict):
            out.append({"price": x.get("price"), "size": x.get("size")})
        else:
            out.append(
                {
                    "price": _get(x, "price"),
                    "size": _get(x, "size"),
                }
            )
    return [x for x in out if x["price"] is not None and x["size"] is not None]


class PolymarketClient:
    """Public data only. Paper-safe."""

    def __init__(self, gamma_url: str, clob_url: str, ws_url: str) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.ws_url = ws_url
        self._sdk = None

    async def _public(self):
        if self._sdk is None:
            from polymarket import AsyncPublicClient

            self._sdk = AsyncPublicClient()
        return self._sdk

    async def list_markets(self, *, closed: bool = False, limit: int = 50) -> list[Market]:
        """Return top-`limit` markets by liquidity then volume from a larger pool.

        The official SDK Market model omits liquidity/volume, so unsorted SDK pages
        look like liquidity=0 and starve the scanner. Universe selection uses Gamma HTTP.
        """
        pool_target = max(limit * 5, 250)
        out = await self._list_markets_http(closed=closed, limit=pool_target)
        return rank_markets_for_universe(out)[:limit]

    async def _list_markets_http(self, *, closed: bool, limit: int) -> list[Market]:
        import httpx

        # Gamma page size capped ~100; paginate with offset/cursor when needed.
        page_size = min(max(limit, 1), 100)
        out: list[Market] = []
        offset = 0
        async with httpx.AsyncClient(timeout=20.0) as http:
            while len(out) < limit:
                params = {
                    "closed": str(closed).lower(),
                    "limit": str(page_size),
                    "offset": str(offset),
                    "order": "liquidityNum",
                    "ascending": "false",
                }
                url = f"{self.gamma_url}/markets/keyset"
                r = await http.get(url, params=params)
                if r.status_code >= 400:
                    # Fallback: volume sort if liquidityNum order unsupported
                    params["order"] = "volume24hr"
                    r = await http.get(url, params=params)
                if r.status_code >= 400:
                    r = await http.get(f"{self.gamma_url}/markets", params=params)
                r.raise_for_status()
                data = r.json()
                rows = data if isinstance(data, list) else data.get("markets") or data.get("data") or data.get("items") or []
                if not rows:
                    break
                for obj in rows:
                    m = market_from_sdk(obj)
                    if m:
                        out.append(m)
                    if len(out) >= limit:
                        break
                if len(rows) < page_size:
                    break
                offset += page_size
        return rank_markets_for_universe(out)[:limit]

    async def get_order_book(self, token_id: str, market_id: str = "") -> OrderBook:
        try:
            client = await self._public()
            book = await client.get_order_book(token_id)
            return self._sdk_book(book, token_id, market_id)
        except Exception:
            return await self._book_http(token_id, market_id)

    def _sdk_book(self, book: Any, token_id: str, market_id: str) -> OrderBook:
        tick = float(_get(book, "tick_size", "tickSize", default=0.01) or 0.01)
        mos = float(_get(book, "min_order_size", "minOrderSize", default=1) or 1)
        neg = bool(_get(book, "neg_risk", "negRisk", default=False))
        h = _get(book, "hash")
        return book_from_raw(
            token_id,
            _book_levels(book, "bids"),
            _book_levels(book, "asks"),
            market_id=market_id,
            tick_size=tick,
            min_order_size=mos,
            neg_risk=neg,
            book_hash=str(h) if h else None,
        )

    async def _book_http(self, token_id: str, market_id: str) -> OrderBook:
        import httpx

        async with httpx.AsyncClient(timeout=20.0) as http:
            r = await http.get(f"{self.clob_url}/book", params={"token_id": token_id})
            r.raise_for_status()
            data = r.json()
        tick = float(data.get("tick_size") or 0.01)
        mos = float(data.get("min_order_size") or 1)
        return book_from_raw(
            token_id,
            data.get("bids") or [],
            data.get("asks") or [],
            market_id=market_id,
            tick_size=tick,
            min_order_size=mos,
            neg_risk=bool(data.get("neg_risk")),
            book_hash=data.get("hash"),
        )

    async def subscribe_books(self, token_ids: list[str]) -> AsyncIterator[OrderBook]:
        """Yield book updates. REST fallback if SDK stream fails."""
        try:
            client = await self._public()
            from polymarket.streams import MarketSpec

            async with await client.subscribe(MarketSpec(token_ids=token_ids)) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "book":
                        payload = getattr(event, "payload", event)
                        tid = str(_get(payload, "token_id", "tokenId", default="") or "")
                        yield self._sdk_book(payload, tid, "")
                    elif getattr(event, "type", None) in ("book",):
                        yield self._sdk_book(event, token_ids[0], "")
            return
        except Exception:
            pass
        while True:
            for tid in token_ids:
                try:
                    yield await self.get_order_book(tid)
                except Exception:
                    continue
            await asyncio.sleep(2.0)

    async def aclose(self) -> None:
        sdk = self._sdk
        self._sdk = None
        if sdk is None:
            return
        close = getattr(sdk, "aclose", None) or getattr(sdk, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result
