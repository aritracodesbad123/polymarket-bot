"""A removed CLOB book is not an API outage. A hung call is."""

import time

import httpx
import pytest

from app.broker.paper import PaperPosition
from app.main import TradingApp
from app.market_data.client import BookNotFound, PolymarketClient
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


def _plant(app: TradingApp, token_id: str = "tok", market_id: str = "4761828") -> None:
    app.paper._positions[token_id] = PaperPosition(
        token_id=token_id,
        market_id=market_id,
        shares=10.0,
        avg_price=0.50,
        category="other",
    )


def _events(app: TradingApp, kind: str):
    return app.db.query(
        "SELECT kind, message, payload_json FROM system_events WHERE kind=? ORDER BY id",
        (kind,),
    )


async def _no_market(_market_id: str):
    return None


def _bid_book(price: float) -> OrderBook:
    return OrderBook(
        token_id="tok",
        market_id="4761828",
        bids=[BookLevel(price=price, size=10_000)],
        asks=[BookLevel(price=price + 0.02, size=10_000)],
    )


def _http_status(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://clob.test/book")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(str(code), request=req, response=resp)


def _closed_unresolved():
    return market(
        market_id="4761828",
        yes_token_id="tok",
        no_token_id="no-tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.42,
        no_price=0.58,
    )


def _resolved():
    return market(
        market_id="4761828",
        yes_token_id="tok",
        no_token_id="no-tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.0,
        no_price=1.0,
    )


@pytest.mark.asyncio
async def test_held_book_404_does_not_count_and_flags_when_unresolved(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    app._last_marks["tok"] = 0.31

    async def book(_token_id: str, _market_id: str = ""):
        raise BookNotFound(_token_id)

    async def get_market(_market_id: str):
        return _closed_unresolved()

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = get_market  # type: ignore[method-assign]

    marks: dict[str, float] = {}
    await app._review_holdings(marks)
    await app._review_holdings({})

    assert app.risk._api_failures == 0
    assert not app.repo.state().halted
    pos = app.paper._positions["tok"]
    assert pos.book_gone
    assert pos.shares == pytest.approx(10.0)
    assert marks["tok"] == pytest.approx(0.31)
    assert pos.last_mark == pytest.approx(0.31)
    gone = _events(app, "HOLDING_BOOK_GONE")
    assert len(gone) == 2
    assert "last_mark=0.310000" in gone[0]["message"]
    assert _events(app, "HOLDING_SETTLE") == []


@pytest.mark.asyncio
async def test_held_book_404_settles_when_gamma_resolves(tmp_path):
    """The pre-check can miss a resolution. The 404 path looks Gamma up again."""
    app = _app(tmp_path)
    _plant(app)
    seen = {"n": 0}

    async def book(_token_id: str, _market_id: str = ""):
        return _bid_book(0.4)

    async def get_market(_market_id: str):
        seen["n"] += 1
        # 1: book still live. 2: pre-check on the 404 cycle, still unresolved.
        # 3: the 404 path asks Gamma again and the outcome is pinned.
        if seen["n"] < 3:
            return None
        return _resolved()

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = get_market  # type: ignore[method-assign]

    # First pass: Gamma has nothing, the book is still there. No settle.
    await app._review_holdings({})
    assert "tok" in app.paper._positions

    async def gone(_token_id: str, _market_id: str = ""):
        raise _http_status(404)

    app.data.get_order_book = gone  # type: ignore[method-assign]
    await app._review_holdings({})

    assert app.risk._api_failures == 0
    assert "tok" not in app.paper._positions
    assert len(_events(app, "HOLDING_SETTLE")) == 1
    assert _events(app, "HOLDING_BOOK_GONE") == []


@pytest.mark.asyncio
async def test_thirteen_dead_books_do_not_halt(tmp_path):
    app = _app(tmp_path)
    for i in range(13):
        _plant(app, token_id=f"tok{i}", market_id=f"m{i}")

    async def book(token_id: str, _market_id: str = ""):
        raise BookNotFound(token_id)

    async def get_market(market_id: str):
        return market(
            market_id=market_id,
            yes_token_id="unused",
            closed=True,
            active=False,
            status="closed",
            yes_price=0.40,
            no_price=0.60,
        )

    app.data.get_order_book = book  # type: ignore[method-assign]
    app.data.get_market = get_market  # type: ignore[method-assign]
    await app._review_holdings({})
    assert app.risk._api_failures == 0
    assert not app.repo.state().halted
    assert len(_events(app, "HOLDING_BOOK_GONE")) == 13
    assert len(app.paper._positions) == 13


@pytest.mark.asyncio
async def test_held_book_5xx_and_timeout_count_as_api_failures(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    app.data.get_market = _no_market  # type: ignore[method-assign]

    async def server_error(_token_id: str, _market_id: str = ""):
        raise _http_status(500)

    app.data.get_order_book = server_error  # type: ignore[method-assign]
    await app._review_holdings({})
    assert app.risk._api_failures == 1
    assert not app.paper._positions["tok"].book_gone
    assert _events(app, "HOLDING_BOOK_GONE") == []
    assert not app.repo.state().halted

    async def timed_out(_token_id: str, _market_id: str = ""):
        raise TimeoutError("order book timeout")

    app.data.get_order_book = timed_out  # type: ignore[method-assign]
    for _ in range(7):
        await app._review_holdings({})
    assert app.repo.state().halted
    assert app.repo.state().halt_reason == "repeated_api_failures"
    assert app.paper._positions["tok"].shares == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_hung_order_book_times_out_and_counts(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    app.data.timeout_s = 0.05
    app.data.get_market = _no_market  # type: ignore[method-assign]

    class Hung:
        async def get_order_book(self, _token_id: str):
            await __import__("asyncio").sleep(30)

    app.data._sdk = Hung()
    started = time.monotonic()
    await app._review_holdings({})
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert app.risk._api_failures == 1
    assert not app.paper._positions["tok"].book_gone
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_hung_gamma_times_out_and_counts(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    app.data.timeout_s = 0.05

    async def hang(_market_id: str):
        await __import__("asyncio").sleep(30)

    async def book(_token_id: str, _market_id: str = ""):
        return _bid_book(0.55)

    app.data._get_market = hang  # type: ignore[method-assign]
    app.data.get_order_book = book  # type: ignore[method-assign]
    started = time.monotonic()
    await app._review_holdings({})
    assert time.monotonic() - started < 2.0
    assert app.risk._api_failures == 1
    assert "tok" in app.paper._positions
    assert not app.paper._positions["tok"].book_gone


@pytest.mark.asyncio
async def test_screening_404_skips_without_api_failure(tmp_path):
    app = _app(tmp_path)
    m = market()

    async def book(token_id: str, _market_id: str = ""):
        raise BookNotFound(token_id)

    app.data.get_order_book = book  # type: ignore[method-assign]
    used = await app._consider(m, [], {})
    assert used is False
    assert app.risk._api_failures == 0

    async def server_error(token_id: str, _market_id: str = ""):
        raise _http_status(503)

    app.data.get_order_book = server_error  # type: ignore[method-assign]
    used = await app._consider(m, [], {})
    assert used is False
    assert app.risk._api_failures == 1


@pytest.mark.asyncio
async def test_missing_no_book_does_not_drop_the_yes_book(tmp_path):
    app = _app(tmp_path)
    m = market()

    async def book(token_id: str, _market_id: str = ""):
        if token_id == m.no_token_id:
            raise _http_status(404)
        return OrderBook(
            token_id=token_id,
            market_id=m.market_id,
            bids=[BookLevel(price=0.40, size=100)],
            asks=[BookLevel(price=0.42, size=100)],
        )

    app.data.get_order_book = book  # type: ignore[method-assign]
    yes, no = await app._books(m)
    assert yes.token_id == m.yes_token_id
    assert no is None
    assert app.risk._api_failures == 0


@pytest.mark.asyncio
async def test_halted_cycle_logs_heartbeat_and_does_not_clear(tmp_path, caplog):
    import logging

    app = _app(tmp_path)
    _plant(app)
    app.repo.halt("repeated_api_failures")
    row = app.db.query_one("SELECT updated_at FROM system_state WHERE id=1")
    since = row["updated_at"]
    called = {"book": 0}

    async def book(*_a, **_k):
        called["book"] += 1
        raise AssertionError("halted cycle must not fetch books")

    app.data.get_order_book = book  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="polygrok"):
        await app.cycle()
        await app.cycle()
    assert f"HALTED reason=repeated_api_failures since={since}" in caplog.text
    assert caplog.text.count("HALTED reason=repeated_api_failures") >= 2
    assert app.repo.state().halted
    assert app.repo.state().halt_reason == "repeated_api_failures"
    assert called["book"] == 0
    beats = _events(app, "HALTED")
    assert len(beats) == 2
    assert "tok" in app.paper._positions


@pytest.mark.asyncio
async def test_survival_halt_is_not_auto_cleared(tmp_path, caplog):
    import logging

    app = _app(tmp_path)
    app.repo.halt("weekly_equity_stop")
    app.risk.note_api_ok()
    with caplog.at_level(logging.WARNING, logger="polygrok"):
        await app.cycle()
    assert "HALTED reason=weekly_equity_stop since=" in caplog.text
    assert app.repo.state().halted
    assert app.repo.state().halt_reason == "weekly_equity_stop"


def test_api_ok_resets_counter_halt_stays_operator_cleared(tmp_path):
    app = _app(tmp_path)
    for _ in range(7):
        app.risk.note_api_failure()
    assert not app.repo.state().halted
    app.risk.note_api_ok()
    assert app.risk._api_failures == 0
    for _ in range(8):
        app.risk.note_api_failure()
    assert app.repo.state().halt_reason == "repeated_api_failures"
    app.risk.note_api_ok()
    assert app.risk._api_failures == 0
    assert app.repo.state().halted
    app.repo.resume_paper()
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_last_mark_survives_restart(tmp_path):
    app = _app(tmp_path)
    _plant(app)
    app.data.get_market = _no_market  # type: ignore[method-assign]

    async def book(_token_id: str, _market_id: str = ""):
        return _bid_book(0.60)

    app.data.get_order_book = book  # type: ignore[method-assign]
    marks: dict[str, float] = {}
    await app._review_holdings(marks)
    assert marks["tok"] == pytest.approx(0.60)

    async def gone(_token_id: str, _market_id: str = ""):
        raise BookNotFound("tok")

    app.data.get_order_book = gone  # type: ignore[method-assign]
    app.data.get_market = _no_market  # type: ignore[method-assign]
    held: dict[str, float] = {}
    await app._review_holdings(held)
    assert held["tok"] == pytest.approx(0.60)
    app.portfolio.snapshot(held)

    restarted = TradingApp(app.settings)
    pos = restarted.paper._positions["tok"]
    assert pos.last_mark == pytest.approx(0.60)
    assert pos.book_gone
    assert pos.shares == pytest.approx(10.0)
    assert restarted.paper.equity() == pytest.approx(app.paper.equity(held))


@pytest.mark.asyncio
async def test_rest_404_is_book_not_found_and_hung_sdk_times_out(monkeypatch):
    class SdkDown:
        async def get_order_book(self, _token_id: str):
            raise RuntimeError("sdk down")

    class Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise _http_status(self.status_code)

        def json(self):
            return {"bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.42", "size": "10"}]}

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            self.code = 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Resp(self.code)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    client = PolymarketClient("http://gamma.test", "http://clob.test", "ws://x")
    client._sdk = SdkDown()
    with pytest.raises(BookNotFound):
        await client.get_order_book("tok", "m1")

    class ServerError(Client):
        def __init__(self, *args, **kwargs) -> None:
            self.code = 500

    monkeypatch.setattr(httpx, "AsyncClient", ServerError)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_order_book("tok", "m1")

    class Hung:
        async def get_order_book(self, _token_id: str):
            await __import__("asyncio").sleep(30)

    client._sdk = Hung()
    client.timeout_s = 0.05
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await client.get_order_book("tok", "m1")
    assert time.monotonic() - started < 1.0
