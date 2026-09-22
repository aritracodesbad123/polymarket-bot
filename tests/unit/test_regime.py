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


def test_cushion_defends_but_is_not_the_spend_budget():
    """Half-cushion still tightens. It does not DIE under the $10 session budget."""
    eng = RegimeEngine(_settings(api_die_cushion_usd=0.50, ai_session_budget_usd=10.0))
    eng.note_ai_call(40)  # 40 * 0.02 = 0.80, over half of 0.50, under budget
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "DEFEND"
    assert st.screening_allowed
    assert st.reason.startswith("burn_half_cushion")
    assert eng.max_ai_calls("DEFEND") == 3


def test_regime_die_on_kill_floor():
    eng = RegimeEngine(_settings())
    # floor = 50 * 0.8 = 40
    st = eng.update(equity=39.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("kill_floor")


def test_zero_burn_startup_does_not_die_and_cushion_zero_is_not_a_budget():
    """Zero burn stays ATTACK. Cushion 0 does not make the first paid call DIE."""
    eng = RegimeEngine(
        _settings(
            api_die_cushion_usd=0.0,
            ai_session_budget_usd=10.0,
            estimated_usd_per_ai_call=0.02,
        )
    )
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "ATTACK"
    assert st.screening_allowed
    assert eng.session_ai_cost_usd == 0.0
    eng.note_ai_call(1)
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "ATTACK"
    assert st.screening_allowed
    assert not st.reason.startswith("api_burn")
    # Cushion 0 also does not hair-trigger DEFEND on a small dip.
    dipped = eng.update(equity=49.0, unrealized_pnl=-0.6)
    assert dipped.mode == "ATTACK"
    assert dipped.screening_allowed
