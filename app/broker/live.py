"""Live CLOB broker. Fail closed. Never submit without live authorization."""

from __future__ import annotations

from datetime import datetime, timezone

from app.broker.models import OrderRecord, OrderRequest, OrderStatus
from app.config import Settings, live_credentials_present, load_live_credentials
from app.market_data.models import OrderBook
from app.risk.authorization import LIVE_CONFIRMATION_PHRASE, auth_from_state
from app.storage.repositories import Repositories
from app.strategy.evaluator import Decision


class LiveLockedError(PermissionError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("live trading locked: " + ",".join(reasons))
        self.reasons = reasons


class LiveBroker:
    name = "live"

    def __init__(self, settings: Settings, repo: Repositories) -> None:
        self.settings = settings
        self.repo = repo
        self._client = None

    def authorize_now(self) -> object:
        st = self.repo.state()
        act = self.repo.latest_activation()
        phrase = act["operator_confirmation"] if act else None
        return auth_from_state(
            now=datetime.now(timezone.utc),
            paper_trading_started_at=st.paper_trading_started_at,
            halted=st.halted,
            live_trading_enabled=self.settings.live_trading_enabled,
            has_activation_record=act is not None,
            activation_phrase=phrase,
            has_valid_live_credentials=live_credentials_present(),
            trading_mode_env=self.settings.trading_mode,
        )

    def operational(self) -> bool:
        return self.authorize_now().allowed

    async def _secure(self):
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        if self._client is None:
            from polymarket import AsyncSecureClient

            key, wallet = load_live_credentials()
            self._client = await AsyncSecureClient.create(private_key=key, wallet=wallet)
        return self._client

    async def submit(self, req: OrderRequest, book: OrderBook, decision: Decision) -> OrderRecord:
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        client = await self._secure()
        resp = await client.place_limit_order(
            token_id=req.token_id,
            side=req.side,
            price=str(req.price),
            size=str(req.size_shares),
        )
        ok = bool(getattr(resp, "ok", False))
        if not ok:
            return OrderRecord(
                client_order_id=req.client_order_id,
                status=OrderStatus.REJECTED,
                message=str(getattr(resp, "message", "rejected")),
            )
        status_raw = str(getattr(resp, "status", "live") or "live").lower()
        status = {
            "live": OrderStatus.OPEN,
            "matched": OrderStatus.FILLED,
            "delayed": OrderStatus.SUBMITTED,
        }.get(status_raw, OrderStatus.OPEN)
        return OrderRecord(
            client_order_id=req.client_order_id,
            status=status,
            remote_order_id=str(getattr(resp, "order_id", "") or ""),
        )

    async def cancel(self, client_order_id: str) -> OrderRecord:
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        client = await self._secure()
        cancel = getattr(client, "cancel_order", None) or getattr(client, "cancel", None)
        if cancel is None:
            raise LiveLockedError(["cancel_method_unknown"])
        result = cancel(client_order_id)
        if hasattr(result, "__await__"):
            await result
        return OrderRecord(client_order_id=client_order_id, status=OrderStatus.CANCELLED)

    async def get_order(self, client_order_id: str) -> OrderRecord | None:
        return None

    async def open_orders(self) -> list[OrderRecord]:
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        return []

    async def positions(self) -> list[dict]:
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        client = await self._secure()
        pages = client.list_positions()
        page = await pages.first_page()
        items = getattr(page, "items", []) or []
        out = []
        for it in items:
            out.append(
                {
                    "token_id": str(getattr(it, "token_id", "")),
                    "shares": float(getattr(it, "size", 0) or 0),
                    "avg_price": float(getattr(it, "avg_price", 0) or 0),
                    "market_id": str(getattr(it, "condition_id", "") or ""),
                }
            )
        return out

    async def balances(self) -> dict:
        auth = self.authorize_now()
        if not auth.allowed:
            raise LiveLockedError(auth.reasons)
        return {"cash": 0.0, "reserved": 0.0}

    async def reconcile(self) -> str | None:
        auth = self.authorize_now()
        if not auth.allowed:
            return None
        return None

    async def cancel_open_live(self) -> None:
        try:
            opens = await self.open_orders()
        except LiveLockedError:
            return
        for o in opens:
            try:
                await self.cancel(o.client_order_id)
            except Exception:
                continue
