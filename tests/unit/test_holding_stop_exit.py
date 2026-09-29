"""Stop exits close the ticket. A miss is loud and is retried next cycle."""

import json

import pytest

from app.broker.paper import PaperPosition
from app.execution.executor import idempotency_key
from app.main import TradingApp
from app.market_data.models import BookLevel, OrderBook
from tests.conftest import market, settings


def _app(tmp_path) -> TradingApp:
    return TradingApp(
        settings(
            tmp_path,
            paper_latency_ms=0,
            paper_starting_bankroll=5000.0,
            holding_stop_pct=0.25,
        )
    )


def _plant(app: TradingApp) -> None:
    app.paper._positions["tok"] = PaperPosition(
        token_id="tok",
        market_id="4761828",
        shares=38.76,
        avg_price=0.644,
        category="wta-kuzmova-buyukak-2026-09-22",
    )


def _events(app: TradingApp, kind: str):
    return app.db.query(
        "SELECT kind, message, payload_json FROM system_events WHERE kind=? ORDER BY id",
        (kind,),
    )


def _payload(row) -> dict:
    return json.loads(row["payload_json"])


async def _no_market(_market_id: str):
    return None


def _bid_book(price: float, *, ask: float | None = None, last: float | None = None) -> OrderBook:
    bids = [BookLevel(price=price, size=10_000)]
    asks = [BookLevel(price=ask, size=10_000)] if ask is not None else []
    return OrderBook(
        token_id="tok",
        market_id="4761828",
        bids=bids,
        asks=asks,
        last_trade_price=last,
    )


@pytest.mark.asyncio
async def test_position_past_stop_exits_in_one_cycle(tmp_path):
    """One-sided 0.1¢ bid: past the stop, spread filter would have blocked the sell."""
    app = _app(tmp_path)
    _plant(app)
    legacy = idempotency_key(
        "4761828", "tok", "SELL", app.settings.strategy_version, kind="exit"
    )
    app.repo.insert_decision(
        {
            "market_id": "4761828",
            "token_id": "tok",
            "side": "SELL",
            "approved": True,
            "gates": {"exit_reason": {"passed": True, "detail": "stop_loss"}},
            "market_price": 0.001,
            "size_shares": 38.76,
            "strategy_version": app.settings.strategy_version,
            "prompt_version": app.settings.prompt_version,
            "idempotency_key": legacy,
        }
    )
    app.repo.insert_order(
        {
            "client_order_id": "old-rejected-exit",
            "idempotency_key": legacy,
            "broker": "paper",
            "market_id": "4761828",
            "token_id": "tok",
            "side": "SELL",
            "price": 0.001,
            "size_shares": 38.76,
            "status": "REJECTED",
        }
    )

    async def book(_token_id: str, _market_id: str = ""):
        return _bid_book(0.001)

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = _no_market  # type: ignore[method-assign]

    app.portfolio.snapshot({})
    assert len(app.repo.positions()) == 1
    await app._review_holdings({})
    app.portfolio.snapshot({})

    assert "tok" not in app.paper._positions
    assert app.repo.positions() == []
    assert app.paper.realized_pnl < -20
    assert _events(app, "HOLDING_HOLD") == []
    stop = _events(app, "HOLDING_STOP")
    assert len(stop) == 1
    body = _payload(stop[0])
    assert body["mark_source"] == "best_bid"
    assert body["mark"] == pytest.approx(0.001)
    assert "mark_source=best_bid" in stop[0]["message"]
    assert "mark=0.001000" in stop[0]["message"]
    exited = _events(app, "HOLDING_EXIT")
    assert len(exited) == 1
    assert _payload(exited[0])["mark_source"] == "best_bid"
    filled = app.db.query("SELECT status FROM orders WHERE status='FILLED'")
    assert len(filled) == 1


@pytest.mark.asyncio
async def test_stop_exit_fails_without_bid_then_retries(tmp_path):
    app = _app(tmp_path)
    _plant(app)

    async def book(_token_id: str, _market_id: str = ""):
        return OrderBook(token_id="tok", market_id="4761828", last_trade_price=0.001)

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = _no_market  # type: ignore[method-assign]

    await app._review_holdings({})
    await app._review_holdings({})

    assert app.paper._positions["tok"].shares == pytest.approx(38.76)
    fails = _events(app, "HOLDING_EXIT_FAIL")
    assert len(fails) == 2
    assert len(_events(app, "HOLDING_STOP")) == 2
    for row in fails:
        assert "no_bid" in row["message"]
        body = _payload(row)
        assert body["mark_source"] == "last_trade"
        assert body["mark"] == pytest.approx(0.001)
        assert body["fail"]
    assert _events(app, "HOLDING_EXIT") == []
    assert _events(app, "HOLDING_HOLD") == []
    rejected = app.db.query(
        "SELECT status, side FROM orders WHERE status='REJECTED'"
    )
    assert len(rejected) == 2
    assert all(r["side"] == "SELL" for r in rejected)


@pytest.mark.asyncio
async def test_price_gap_from_above_stop_to_zero_exits(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    state = {"book": _bid_book(0.60, ask=0.62)}

    async def book(_token_id: str, _market_id: str = ""):
        return state["book"]

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = _no_market  # type: ignore[method-assign]

    marks: dict[str, float] = {}
    await app._review_holdings(marks)
    assert "tok" in app.paper._positions
    assert marks["tok"] == pytest.approx(0.60)
    assert _events(app, "HOLDING_STOP") == []

    state["book"] = _bid_book(0.0)
    await app._review_holdings(marks)

    assert "tok" not in app.paper._positions
    assert marks["tok"] == pytest.approx(0.0)
    stop = _events(app, "HOLDING_STOP")
    assert len(stop) == 1
    assert _payload(stop[0])["mark_source"] == "best_bid"
    assert _payload(stop[0])["mark"] == pytest.approx(0.0)
    assert len(_events(app, "HOLDING_EXIT")) == 1
    assert app.paper.realized_pnl < -20


@pytest.mark.asyncio
async def test_resolved_market_settles_and_drops_the_open_ticket(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    seen = {"book": 0}

    async def book(_token_id: str, _market_id: str = ""):
        seen["book"] += 1
        return OrderBook(token_id="tok", market_id="4761828")

    resolved = market(
        market_id="4761828",
        yes_token_id="tok",
        no_token_id="no-tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.0,
        no_price=1.0,
    )

    async def get_market(_market_id: str):
        return resolved

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = get_market  # type: ignore[method-assign]

    app.portfolio.snapshot({})
    assert len(app.repo.positions()) == 1
    cash_before = app.paper.cash
    await app._review_holdings({})
    app.portfolio.snapshot({})

    assert seen["book"] == 0
    assert "tok" not in app.paper._positions
    assert app.repo.positions() == []
    assert app.paper.cash == pytest.approx(cash_before)
    assert app.paper.realized_pnl == pytest.approx(-38.76 * 0.644)
    settled = _events(app, "HOLDING_SETTLE")
    assert len(settled) == 1
    body = _payload(settled[0])
    assert body["mark_source"] == "resolution"
    assert body["mark"] == pytest.approx(0.0)
    assert body["outcome"] == "NO"
    assert "mark_source=resolution" in settled[0]["message"]
    fills = app.db.query("SELECT side, shares, price FROM fills")
    assert len(fills) == 1
    assert fills[0]["side"] == "SELL"
    assert fills[0]["shares"] == pytest.approx(38.76)
    assert fills[0]["price"] == pytest.approx(0.0)
    assert _events(app, "HOLDING_EXIT_FAIL") == []
