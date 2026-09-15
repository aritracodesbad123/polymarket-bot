from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from app.market_data.models import OrderBook
from app.risk.authorization import LiveAuthResult
from app.strategy.evaluator import Decision


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderRequest(BaseModel):
    client_order_id: str
    idempotency_key: str
    market_id: str
    token_id: str
    side: str
    price: float
    size_shares: float
    order_type: str = "GTC"
    decision_id: int | None = None


class OrderRecord(BaseModel):
    client_order_id: str
    status: OrderStatus
    remote_order_id: str | None = None
    filled_shares: float = 0.0
    avg_fill_price: float = 0.0
    message: str = ""
    remaining: float = 0.0


class Broker(Protocol):
    name: str

    async def submit(self, req: OrderRequest, book: OrderBook, decision: Decision) -> OrderRecord: ...
    async def cancel(self, client_order_id: str) -> OrderRecord: ...
    async def get_order(self, client_order_id: str) -> OrderRecord | None: ...
    async def open_orders(self) -> list[OrderRecord]: ...
    async def positions(self) -> list[dict]: ...
    async def balances(self) -> dict: ...
    async def reconcile(self) -> str | None: ...
    def operational(self) -> bool: ...
