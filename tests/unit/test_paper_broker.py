import pytest

from app.broker.paper import PaperBroker
from app.broker.models import OrderRequest
from app.strategy.evaluator import Decision
from tests.conftest import book


@pytest.mark.asyncio
async def test_paper_walks_asks_not_midpoint():
    br = PaperBroker(cash=1000, latency_ms=0)
    b = book(ask=0.40, bid=0.30, ask_size=100)
    req = OrderRequest(
        client_order_id="c1",
        idempotency_key="k1",
        market_id="m1",
        token_id="yes1",
        side="BUY",
        price=0.40,
        size_shares=10,
    )
    d = Decision(approved=True, reject_reason=None, gates=[], market_id="m1", category="politics")
    rec = await br.submit(req, b, d)
    assert rec.status.value in ("FILLED", "PARTIALLY_FILLED")
    assert rec.avg_fill_price == pytest.approx(0.40, abs=1e-9)
    assert rec.avg_fill_price != pytest.approx(0.35, abs=0.001)  # not midpoint


@pytest.mark.asyncio
async def test_paper_partial_and_gtc():
    br = PaperBroker(cash=1000, latency_ms=0)
    b = book(ask=0.40, ask_size=2)
    req = OrderRequest(
        client_order_id="c2",
        idempotency_key="k2",
        market_id="m1",
        token_id="yes1",
        side="BUY",
        price=0.42,
        size_shares=20,
    )
    d = Decision(approved=True, reject_reason=None, gates=[], market_id="m1")
    rec = await br.submit(req, b, d)
    assert rec.status.value in ("PARTIALLY_FILLED", "OPEN")
    assert rec.remaining > 0 or rec.status.value == "OPEN"
