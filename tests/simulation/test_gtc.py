from app.broker.paper import PaperBroker
from app.market_data.models import BookLevel, OrderBook


def test_resting_fill_on_new_book():
    br = PaperBroker(1000, 0)
    # empty asks → rest
    empty = OrderBook(token_id="yes1", bids=[BookLevel(price=0.3, size=10)])
    import asyncio
    from app.broker.models import OrderRequest
    from app.strategy.evaluator import Decision

    req = OrderRequest(
        client_order_id="c",
        idempotency_key="k",
        market_id="m1",
        token_id="yes1",
        side="BUY",
        price=0.4,
        size_shares=5,
    )
    d = Decision(approved=True, reject_reason=None, gates=[], market_id="m1")
    rec = asyncio.run(br.submit(req, empty, d))
    assert rec.status.value == "OPEN"
    fresh = OrderBook(
        token_id="yes1",
        asks=[BookLevel(price=0.39, size=5)],
        bids=[BookLevel(price=0.3, size=10)],
    )
    fills = br.on_book(fresh)
    assert fills
    assert fills[0].status.value == "FILLED"
    assert br._positions["yes1"].shares == 5
