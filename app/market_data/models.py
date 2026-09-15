from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    market_id: str = ""
    bids: list[BookLevel] = Field(default_factory=list)  # best first (high)
    asks: list[BookLevel] = Field(default_factory=list)  # best first (low)
    tick_size: float = 0.01
    min_order_size: float = 1.0
    neg_risk: bool = False
    hash: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    last_trade_price: float | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (now - fetched).total_seconds()

    def ask_depth_usd(self) -> float:
        return sum(lvl.price * lvl.size for lvl in self.asks)

    def bid_depth_usd(self) -> float:
        return sum(lvl.price * lvl.size for lvl in self.bids)


class Market(BaseModel):
    market_id: str
    condition_id: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    question: str = ""
    description: str = ""
    resolution_criteria: str = ""
    close_time: datetime | None = None
    resolution_time: datetime | None = None
    category: str = ""
    event_id: str | None = None
    correlation_group: str = ""
    neg_risk: bool = False
    tick_size: float = 0.01
    min_order_size: float = 1.0
    status: str = "active"
    volume: float = 0.0
    liquidity: float = 0.0
    yes_price: float | None = None
    no_price: float | None = None
    active: bool = True
    closed: bool = False
    paused: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    def hours_to_resolution(self, now: datetime | None = None) -> float | None:
        now = now or utcnow()
        end = self.close_time or self.resolution_time
        if end is None:
            return None
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - now).total_seconds() / 3600.0
