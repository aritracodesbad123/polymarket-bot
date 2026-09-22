"""Paper BUY then SELL realizes PnL against bids."""

import pytest

from app.broker.models import OrderRequest
from app.broker.paper import PaperBroker, PaperPosition
from app.portfolio.portfolio import Portfolio
from app.storage.repositories import Repositories
from app.strategy.evaluator import Decision
from tests.conftest import book, db


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


def _order(cid: str, market_id: str, token_id: str, side: str, price: float, shares: float) -> OrderRequest:
    return OrderRequest(
        client_order_id=cid,
        idempotency_key=cid,
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size_shares=shares,
    )


@pytest.mark.asyncio
async def test_full_sell_clears_position_row_and_equity_mark(tmp_path):
    """BUY then full SELL must not leave a ghost row that equity marks again.

    Market 4798928 is the week-2 paper ghost: sale cash stayed, and the
    position row was still marked.
    """
    d, repo = db(tmp_path)
    br = PaperBroker(cash=100.0, latency_ms=0)
    port = Portfolio(br, repo)
    decision = Decision(
        approved=True, reject_reason=None, gates=[], market_id="4798928", category="sports"
    )
    other = Decision(
        approved=True, reject_reason=None, gates=[], market_id="m-open", category="sports"
    )
    await br.submit(
        _order("b-ghost", "4798928", "sun", "BUY", 0.40, 10),
        book(token_id="sun", ask=0.40, bid=0.38, ask_size=100),
        decision,
    )
    await br.submit(
        _order("b-open", "m-open", "other", "BUY", 0.50, 4),
        book(token_id="other", ask=0.50, bid=0.48, ask_size=100),
        other,
    )
    port.snapshot({"sun": 0.40, "other": 0.50})
    assert any(r["market_id"] == "4798928" for r in repo.positions())

    await br.submit(
        _order("s-ghost", "4798928", "sun", "SELL", 0.30, 10),
        book(token_id="sun", ask=0.32, bid=0.30, bid_size=100),
        decision,
    )
    closed_mark = 0.99
    snap = port.snapshot({"sun": closed_mark, "other": 0.55})

    ghost_rows = d.query("SELECT * FROM positions WHERE market_id = ?", ("4798928",))
    assert ghost_rows == []
    open_rows = repo.positions()
    assert len(open_rows) == 1
    assert open_rows[0]["market_id"] == "m-open"
    assert open_rows[0]["shares"] == pytest.approx(4)

    # Equity is cash + the still-open ticket only. The closed mark must not add.
    assert br.reserved == pytest.approx(0.0)
    assert snap["equity"] == pytest.approx(br.cash + 4 * 0.55)
    assert snap["exposure"] == pytest.approx(4 * 0.55)
    assert snap["equity"] < br.cash + 4 * 0.55 + 10 * closed_mark - 1.0

    restored = PaperBroker(cash=100.0, latency_ms=0)
    Portfolio(restored, repo).hydrate_paper(100.0)
    assert "sun" not in restored._positions
    assert restored.cash == pytest.approx(br.cash)
    assert restored.equity({"sun": closed_mark, "other": 0.55}) == pytest.approx(br.cash + 4 * 0.55)


@pytest.mark.asyncio
async def test_partial_sell_reduces_size_and_cost_basis(tmp_path):
    _d, repo = db(tmp_path)
    br = PaperBroker(cash=100.0, latency_ms=0)
    port = Portfolio(br, repo)
    decision = Decision(
        approved=True, reject_reason=None, gates=[], market_id="m1", category="sports"
    )
    await br.submit(
        _order("b1", "m1", "yes1", "BUY", 0.40, 10),
        book(ask=0.40, bid=0.38, ask_size=100),
        decision,
    )
    buy_avg = br._positions["yes1"].avg_price
    await br.submit(
        _order("s1", "m1", "yes1", "SELL", 0.36, 4),
        book(ask=0.38, bid=0.36, bid_size=100),
        decision,
    )
    pos = br._positions["yes1"]
    assert pos.shares == pytest.approx(6)
    assert pos.avg_price == pytest.approx(buy_avg)
    assert pos.shares * pos.avg_price == pytest.approx(6 * buy_avg)

    mark = 0.70
    snap = port.snapshot({"yes1": mark})
    rows = repo.positions()
    assert len(rows) == 1
    assert rows[0]["market_id"] == "m1"
    assert rows[0]["shares"] == pytest.approx(6)
    assert rows[0]["avg_price"] == pytest.approx(buy_avg)
    assert snap["equity"] == pytest.approx(br.cash + 6 * mark)
    assert snap["exposure"] == pytest.approx(6 * mark)


def test_equity_ignores_flat_position():
    br = PaperBroker(cash=4988.0, latency_ms=0)
    br._positions["ghost"] = PaperPosition(
        token_id="ghost", market_id="4798928", shares=0.0, avg_price=0.42
    )
    br._positions["open"] = PaperPosition(
        token_id="open", market_id="m2", shares=10, avg_price=0.40
    )
    marks = {"ghost": 0.99, "open": 0.50}
    assert br.equity(marks) == pytest.approx(4988.0 + 5.0)
    assert br.exposure(marks) == pytest.approx(5.0)


def test_snapshot_rolls_back_when_position_delete_fails(tmp_path, monkeypatch):
    _d, repo = db(tmp_path)
    br = PaperBroker(cash=50.0, latency_ms=0)
    br._positions["yes1"] = PaperPosition(
        token_id="yes1", market_id="4798928", shares=5, avg_price=0.40
    )

    def boom(_self, _token_ids):
        raise RuntimeError("disk")

    monkeypatch.setattr(Repositories, "delete_positions_except", boom)
    with pytest.raises(RuntimeError):
        Portfolio(br, repo).snapshot({"yes1": 0.40})
    assert repo.latest_portfolio() is None
    assert repo.positions() == []
