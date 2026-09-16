from app.config import Settings
from app.strategy.holding_review import diagnose_holding
from tests.conftest import book


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
