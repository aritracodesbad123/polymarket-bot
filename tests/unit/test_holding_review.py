from app.config import Settings
from app.market_data.models import BookLevel, OrderBook
from app.strategy.holding_review import diagnose_holding, resolution_price
from tests.conftest import book, market


def _settings(**kw) -> Settings:
    base = dict(
        db_path=":memory:",
        holding_stop_pct=0.25,
        holding_thesis_edge=0.02,
        holding_max_hours=48.0,
    )
    base.update(kw)
    return Settings(**base)


def test_holding_ok():
    v = diagnose_holding(
        shares=10,
        avg_price=0.20,
        token_id="t1",
        market_id="m1",
        book=book(bid=0.21, ask=0.22),
        entry_p=0.30,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "ok"


def test_holding_stop_loss():
    # entry 0.40, mark 0.28 → unreal_pct = (0.28-0.40)/0.40 = -0.30
    v = diagnose_holding(
        shares=10,
        avg_price=0.40,
        token_id="t1",
        market_id="m1",
        book=book(bid=0.28, ask=0.30),
        entry_p=0.55,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "stop_loss"


def test_holding_thesis_broken():
    # entry_p 0.40, mark 0.45 → edge_now = -0.05 < -0.02
    v = diagnose_holding(
        shares=10,
        avg_price=0.38,
        token_id="t1",
        market_id="m1",
        book=book(bid=0.45, ask=0.46),
        entry_p=0.40,
        entry_ts=None,
        settings=_settings(holding_stop_pct=0.99),
    )
    assert v.reason == "thesis_broken"


def test_near_zero_bid_is_a_stop_and_names_the_mark():
    v = diagnose_holding(
        shares=38.76,
        avg_price=0.644,
        token_id="tok",
        market_id="4761828",
        book=OrderBook(
            token_id="tok",
            bids=[BookLevel(price=0.001, size=5000)],
        ),
        entry_p=0.70,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "stop_loss"
    assert v.mark_source == "best_bid"
    assert v.mark == 0.001
    assert "mark_source=best_bid" in v.detail


def test_zero_bid_is_a_stop_mark():
    v = diagnose_holding(
        shares=10,
        avg_price=0.644,
        token_id="tok",
        market_id="4761828",
        book=OrderBook(token_id="tok", bids=[BookLevel(price=0.0, size=100)]),
        entry_p=0.70,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "stop_loss"
    assert v.mark == 0.0
    assert v.mark_source == "best_bid"


def test_last_trade_marks_an_empty_book():
    v = diagnose_holding(
        shares=10,
        avg_price=0.644,
        token_id="tok",
        market_id="4761828",
        book=OrderBook(token_id="tok", last_trade_price=0.001),
        entry_p=0.70,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "stop_loss"
    assert v.mark_source == "last_trade"
    assert v.mark == 0.001


def test_missing_mark_does_not_invent_a_stop():
    v = diagnose_holding(
        shares=10,
        avg_price=0.644,
        token_id="tok",
        market_id="4761828",
        book=OrderBook(token_id="tok"),
        entry_p=0.70,
        entry_ts=None,
        settings=_settings(),
    )
    assert v.reason == "ok"
    assert v.mark is None
    assert v.mark_source == "none"
    assert v.detail == "no_mark"


def test_resolution_price_pinned_loser():
    m = market(
        market_id="4761828",
        yes_token_id="tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.0,
        no_price=1.0,
    )
    assert resolution_price(m, "tok") == 0.0
    dust = market(
        market_id="4761828",
        yes_token_id="tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.001,
        no_price=0.999,
    )
    assert resolution_price(dust, "tok") == 0.001


def test_resolution_price_ignores_live_and_unpinned_books():
    live = market(yes_token_id="tok", yes_price=0.0, no_price=1.0)
    assert resolution_price(live, "tok") is None
    mid = market(
        yes_token_id="tok",
        closed=True,
        active=False,
        status="closed",
        yes_price=0.40,
        no_price=0.60,
    )
    assert resolution_price(mid, "tok") is None
