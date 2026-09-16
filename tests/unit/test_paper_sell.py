"""Paper BUY then SELL realizes PnL against bids."""

import pytest

from app.broker.models import OrderRequest
from app.broker.paper import PaperBroker
from app.strategy.evaluator import Decision
from tests.conftest import book


@pytest.mark.asyncio
async def test_paper_buy_then_sell_realizes_pnl():
    br = PaperBroker(cash=100.0, latency_ms=0)
    d = Decision(approved=True, reject_reason=None, gates=[], market_id="m1", category="crypto")
    buy = OrderRequest(
        client_order_id="b1",
        idempotency_key="kb",
        market_id="m1",
        token_id="yes1",
        side="BUY",
        price=0.40,
        size_shares=10,
    )
    rec_b = await br.submit(buy, book(ask=0.40, bid=0.38, ask_size=100), d)
    assert rec_b.filled_shares == pytest.approx(10)
    cash_after_buy = br.cash
    assert cash_after_buy < 100

    sell = OrderRequest(
        client_order_id="s1",
        idempotency_key="ks",
        market_id="m1",
        token_id="yes1",
        side="SELL",
        price=0.30,
        size_shares=10,
    )
    # mark down: best bid 0.30
    rec_s = await br.submit(sell, book(ask=0.32, bid=0.30, bid_size=100), d)
    assert rec_s.status.value == "FILLED"
    assert rec_s.filled_shares == pytest.approx(10)
    assert rec_s.avg_fill_price == pytest.approx(0.30)
    assert not br._positions
    assert br.realized_pnl < 0
    assert br.cash > cash_after_buy


@pytest.mark.asyncio
async def test_paper_sell_rejects_without_position():
    br = PaperBroker(cash=50.0, latency_ms=0)
    d = Decision(approved=True, reject_reason=None, gates=[], market_id="m1")
    sell = OrderRequest(
        client_order_id="s2",
        idempotency_key="ks2",
        market_id="m1",
        token_id="yes1",
        side="SELL",
        price=0.40,
        size_shares=5,
    )
    rec = await br.submit(sell, book(bid=0.40), d)
    assert rec.status.value == "REJECTED"
    assert rec.message == "no_position"
