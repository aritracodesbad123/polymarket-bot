import pytest

from app.config import Settings
from app.risk.regime import RegimeEngine


def _settings(**kw) -> Settings:
    base = dict(
        db_path=":memory:",
        paper_starting_bankroll=50.0,
        estimated_usd_per_ai_call=0.02,
        api_die_cushion_usd=0.50,
        kill_floor_pct=0.20,
        regime_enabled=True,
        min_edge=0.05,
        kelly_multiplier=0.25,
        defend_edge_tighten=0.02,
        defend_kelly_mult=0.5,
        defend_max_grok_calls=3,
        max_grok_calls_per_cycle=8,
    )
    base.update(kw)
    return Settings(**base)


def test_regime_attack_when_healthy():
    eng = RegimeEngine(_settings())
    st = eng.update(equity=50.5, unrealized_pnl=0.1)
    assert st.mode == "ATTACK"


def test_regime_defend_on_unrealized():
    eng = RegimeEngine(_settings())
    st = eng.update(equity=50.0, unrealized_pnl=-0.6)
    assert st.mode == "DEFEND"
    kw = eng.evaluate_kwargs("DEFEND")
    assert kw["min_edge"] == pytest.approx(0.07)
    assert kw["kelly_multiplier"] == pytest.approx(0.125)
    assert eng.max_ai_calls("DEFEND") == 3


def test_regime_die_on_api_burn():
    eng = RegimeEngine(_settings())
    eng.note_ai_call(40)  # 40 * 0.02 = 0.80 > profit(0)+0.50 cushion
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert eng.max_ai_calls("DIE") == 0


def test_regime_die_on_kill_floor():
    eng = RegimeEngine(_settings())
    # floor = 50 * 0.8 = 40
    st = eng.update(equity=39.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("kill_floor")


def test_zero_burn_startup_not_die_first_paid_call_is():
    """Cushion 0 is burn >= profit, but 0 burn and 0 profit must not DIE."""
    eng = RegimeEngine(
        _settings(api_die_cushion_usd=0.0, estimated_usd_per_ai_call=0.02)
    )
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "ATTACK"
    assert eng.session_ai_cost_usd == 0.0
    eng.note_ai_call(1)  # 0.02 >= profit 0
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("api_burn")
    # Profit that covers the call does not DIE.
    eng2 = RegimeEngine(
        _settings(api_die_cushion_usd=0.0, estimated_usd_per_ai_call=0.02)
    )
    eng2.note_ai_call(1)
    covered = eng2.update(equity=50.03, unrealized_pnl=0.0)
    assert covered.mode != "DIE"
