from app.market_data.models import BookLevel, OrderBook
from app.market_data.orderbook import walk_book
from tests.conftest import book


def test_walk_buy_vwap():
    b = OrderBook(
        token_id="t",
        asks=[BookLevel(price=0.40, size=10), BookLevel(price=0.50, size=10)],
        bids=[BookLevel(price=0.39, size=10)],
    )
    f = walk_book(b, "BUY", 15)
    assert f.filled_shares == 15
    assert abs(f.vwap - (10 * 0.4 + 5 * 0.5) / 15) < 1e-12
    assert f.fully_filled
    assert f.slippage_pct > 0


def test_partial_fill_when_thin():
    b = book(ask_size=5)
    f = walk_book(b, "BUY", 100)
    assert not f.fully_filled
    assert f.filled_shares == 10  # two ask levels
    assert f.unfilled_shares == 90


def test_empty_book():
    b = OrderBook(token_id="t")
    f = walk_book(b, "BUY", 10)
    assert f.filled_shares == 0
    assert not f.fully_filled
