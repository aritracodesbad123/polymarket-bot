"""ESTIMATOR_AUTO_SWITCH: LLM↔micro on realized PnL vs burn (Max lock)."""

from __future__ import annotations

import logging

import pytest

from app.main import (
    GEMINI_EDGE_TIGHTEN,
    GEMINI_KELLY_MULTIPLIER,
    GEMINI_MIN_CONFIDENCE,
    GEMINI_MIN_EXEC_EDGE,
    TradingApp,
)
from app.risk.regime import (
    RegimeEngine,
    RegimeState,
    ai_spend_blocked_screening,
    estimator_auto_switch_armed,
    estimator_switch_reason,
    new_screening_allowed,
    screening_path,
)
from tests.conftest import book, estimate, market, settings
from tests.unit.test_regime import _settings


def _auto(**kw):
    base = dict(
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
        estimator="microstructure",
        estimator_auto_switch=True,
    )
    base.update(kw)
    return _settings(**base)


def test_survival_gates_unchanged_under_auto_switch():
    assert GEMINI_EDGE_TIGHTEN == 0.02
    assert GEMINI_MIN_CONFIDENCE == 0.50
    assert GEMINI_MIN_EXEC_EDGE == 0.02
    assert GEMINI_KELLY_MULTIPLIER == 0.125
    s = _auto()
    assert s.min_edge == 0.05
    assert s.kelly_multiplier == 0.25
    assert s.max_spread == 0.06
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90
    assert s.kill_floor_pct == 0.20
    assert s.ai_session_budget_usd == 10.0


def test_auto_switch_armed_only_with_microstructure():
    assert estimator_auto_switch_armed(_auto())
    assert not estimator_auto_switch_armed(_auto(estimator=None))
    assert not estimator_auto_switch_armed(_auto(estimator="off"))
    assert not estimator_auto_switch_armed(
        _auto(estimator="microstructure", estimator_auto_switch=False)
    )


def test_pre_fill_still_dies_at_budget_then_micro_path():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(10)
    st = eng.update(equity=50.0, unrealized_pnl=0.0, realized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("ai_session_budget")
    assert not new_screening_allowed(st)
    assert ai_spend_blocked_screening(st)
    assert screening_path(st, _auto()) == "micro"
    assert estimator_switch_reason(st, "micro") == "ai_session_budget"
    # Burn already at budget: cannot flip LLM back even if realized is huge.
    covered = eng.update(equity=50.0, unrealized_pnl=0.0, realized_pnl=100.0)
    assert covered.mode == "DIE"
    assert not new_screening_allowed(covered)
    assert screening_path(covered, _auto()) == "micro"


def test_post_fill_realized_lt_burn_uses_micro():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(4)
    st = eng.update(
        equity=50.0,
        unrealized_pnl=10.0,  # covering unrealized must not keep LLM
        realized_pnl=2.0,
        has_taken_fills=True,
    )
    assert st.mode != "DIE"
    assert not st.screening_allowed
    assert "realized_lt_burn" in st.reason
    assert screening_path(st, _auto()) == "micro"
    assert estimator_switch_reason(st, "micro") == "realized_lt_burn"


def test_post_fill_realized_gt_burn_flips_llm_on():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(4)
    stopped = eng.update(
        equity=50.0, unrealized_pnl=0.0, realized_pnl=1.0, has_taken_fills=True
    )
    assert screening_path(stopped, _auto()) == "micro"
    resumed = eng.update(
        equity=50.0, unrealized_pnl=0.0, realized_pnl=5.0, has_taken_fills=True
    )
    assert resumed.screening_allowed
    assert resumed.mode != "DIE"
    assert new_screening_allowed(resumed)
    assert screening_path(resumed, _auto()) == "llm"
    assert estimator_switch_reason(resumed, "llm") == "pnl_gt_burn"


def test_post_fill_realized_eq_burn_fail_closed_micro():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(4)
    st = eng.update(
        equity=50.0, unrealized_pnl=99.0, realized_pnl=4.0, has_taken_fills=True
    )
    assert not st.screening_allowed
    assert "realized_eq_burn" in st.reason
    assert screening_path(st, _auto()) == "micro"
    assert estimator_switch_reason(st, "micro") == "realized_eq_burn"


def test_post_fill_burn_exhausted_keeps_micro_even_if_realized_covers():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(10)
    st = eng.update(
        equity=50.0, unrealized_pnl=20.0, realized_pnl=20.0, has_taken_fills=True
    )
    assert st.mode != "DIE"
    assert not st.screening_allowed
    assert "burn_exhausted" in st.reason
    assert screening_path(st, _auto()) == "micro"
    assert estimator_switch_reason(st, "micro") == "burn_exhausted"


def test_realized_unknown_fail_closed_to_micro():
    eng = RegimeEngine(_auto())
    eng.note_ai_call(3)
    st = eng.update(
        equity=50.0, unrealized_pnl=10.0, realized_pnl=None, has_taken_fills=True
    )
    assert not st.screening_allowed
    assert "realized_unknown" in st.reason
    assert screening_path(st, _auto()) == "micro"
    assert estimator_switch_reason(st, "micro") == "realized_unknown"


def test_auto_switch_off_keeps_unrealized_rule():
    eng = RegimeEngine(_auto(estimator_auto_switch=False))
    eng.note_ai_call(4)
    # Realized covers but unrealized does not → legacy stop.
    st = eng.update(
        equity=50.0, unrealized_pnl=1.0, realized_pnl=10.0, has_taken_fills=True
    )
    assert not st.screening_allowed
    assert "unrealized" in st.reason
    assert screening_path(st, _auto(estimator_auto_switch=False)) == "micro"
    # Covering unrealized keeps LLM even past $10 when auto-switch is off.
    eng.note_ai_call(6)  # burn 10
    still = eng.update(
        equity=50.0, unrealized_pnl=10.0, realized_pnl=0.0, has_taken_fills=True
    )
    assert still.screening_allowed


def test_kill_floor_stays_none_not_micro():
    eng = RegimeEngine(_auto(kill_floor_pct=0.20))
    eng.note_ai_call(4)
    st = eng.update(equity=39.0, unrealized_pnl=0.0, realized_pnl=100.0)
    assert st.reason.startswith("kill_floor")
    assert screening_path(st, _auto()) == "none"
    assert not ai_spend_blocked_screening(st)


def test_spend_block_reasons_recognized():
    stop = RegimeState(
        "ATTACK",
        "screening_stop realized_lt_burn 1.0000<burn 4.0000",
        4,
        4.0,
        50,
        50,
        screening_allowed=False,
        has_taken_fills=True,
    )
    exhausted = RegimeState(
        "ATTACK",
        "screening_stop burn_exhausted 10.0000>=10.0000",
        10,
        10.0,
        50,
        50,
        screening_allowed=False,
        has_taken_fills=True,
    )
    assert ai_spend_blocked_screening(stop)
    assert ai_spend_blocked_screening(exhausted)


class _Engine:
    def __init__(self):
        self.calls = 0
        self.provider = "grok"
        self.last_model = "stub"
        self.grok = None
        self.gemini = None

    async def estimate(self, packet):
        self.calls += 1
        return estimate(p=float(packet.implied_probability or 0.5))


class _Research:
    def __init__(self):
        self.calls = 0
        self.blocked = False

    async def gather(self, packet):
        self.calls += 1
        return packet


def _wire(app: TradingApp) -> dict[str, int]:
    calls = {"scan": 0, "review": 0}
    yes = book(token_id="yes1", bid=0.38, ask=0.40, bid_size=8_000.0, ask_size=2_000.0)
    no = book(
        token_id="no1",
        bid=0.60,
        ask=0.62,
        bid_size=2_000.0,
        ask_size=8_000.0,
    )

    async def review(_marks):
        calls["review"] += 1

    async def scan():
        calls["scan"] += 1
        return [(market(), None)]

    async def books(_m):
        return yes, no

    app._review_holdings = review  # type: ignore[method-assign]
    app.scanner.scan = scan  # type: ignore[method-assign]
    app._books = books  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_cycle_flip_llm_on_when_realized_covers(tmp_path, caplog):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
        estimator="microstructure",
        estimator_auto_switch=True,
    )
    app = TradingApp(s)
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app)
    app.repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 2, "price": 0.4})
    app.regime.note_ai_call(4)
    app.risk.note_realized_pnl(5.0)

    app.log.propagate = True
    with caplog.at_level(logging.INFO, logger="polygrok"):
        await app.cycle()

    assert calls["review"] == 1
    assert calls["scan"] == 1
    assert eng.calls == 1
    assert app.research.calls == 1
    assert app.regime.session_ai_calls == 5
    assert app.regime.last is not None
    assert app.regime.last.screening_allowed
    assert "ESTIMATOR_SWITCH" in caplog.text
    assert "to=llm" in caplog.text
    assert "reason=pnl_gt_burn" in caplog.text
    assert "SCREEN provider=micro" not in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds and "grok" in preds[0]["model"]
    events = app.repo.db.query(
        "SELECT message FROM system_events WHERE kind='ESTIMATOR_SWITCH'"
    )
    assert events
    assert "pnl_gt_burn" in events[0]["message"]
    assert "to=llm" in events[0]["message"]


@pytest.mark.asyncio
async def test_cycle_flip_micro_on_realized_lt_burn(tmp_path, caplog):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
        estimator="microstructure",
        estimator_auto_switch=True,
    )
    app = TradingApp(s)
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app)
    app.repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 2, "price": 0.4})
    app.regime.note_ai_call(4)
    app.risk.note_realized_pnl(1.0)

    app.log.propagate = True
    with caplog.at_level(logging.INFO, logger="polygrok"):
        await app.cycle()

    assert calls["review"] == 1
    assert calls["scan"] == 1
    assert eng.calls == 0
    assert app.research.calls == 0
    assert app.regime.session_ai_calls == 4
    assert app.regime.last is not None
    assert "realized_lt_burn" in app.regime.last.reason
    assert "provider=micro" in caplog.text
    assert "reason=realized_lt_burn" in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds and preds[0]["model"] == "micro"


@pytest.mark.asyncio
async def test_cycle_burn_exhausted_stays_micro(tmp_path, caplog):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
        estimator="microstructure",
        estimator_auto_switch=True,
    )
    app = TradingApp(s)
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app)
    app.repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 2, "price": 0.4})
    app.regime.note_ai_call(10)
    app.risk.note_realized_pnl(50.0)

    app.log.propagate = True
    with caplog.at_level(logging.INFO, logger="polygrok"):
        await app.cycle()

    assert calls["scan"] == 1
    assert eng.calls == 0
    assert app.regime.session_ai_calls == 10
    assert "burn_exhausted" in (app.regime.last.reason if app.regime.last else "")
    assert "reason=burn_exhausted" in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds and preds[0]["model"] == "micro"
