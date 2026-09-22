"""Week-two session AI budget: no-fill cap, post-fill screening stop, holdings."""

from __future__ import annotations

import pytest

from app.main import GEMINI_EDGE_TIGHTEN, GEMINI_KELLY_MULTIPLIER, GEMINI_MIN_CONFIDENCE, GEMINI_MIN_EXEC_EDGE, TradingApp
from app.risk.regime import RegimeEngine, new_screening_allowed
from app.storage.db import DatabaseError
from tests.conftest import db, market, settings
from tests.unit.test_regime import _settings


def _budget_settings(**kw):
    base = dict(
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
    )
    base.update(kw)
    return _settings(**base)


def test_gemini_survival_lock_unchanged():
    assert GEMINI_EDGE_TIGHTEN == 0.02
    assert GEMINI_MIN_CONFIDENCE == 0.50
    assert GEMINI_MIN_EXEC_EDGE == 0.02
    assert GEMINI_KELLY_MULTIPLIER == 0.125


def test_zero_burn_startup_allows_screening():
    eng = RegimeEngine(_budget_settings())
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "ATTACK"
    assert st.screening_allowed
    assert st.session_ai_cost_usd == 0.0
    assert new_screening_allowed(st)
    assert eng.max_ai_calls() == 8
    # A fill and a red mark still do not stop screening before any burn.
    red = eng.update(equity=50.0, unrealized_pnl=-4.0, has_taken_fills=True)
    assert red.mode == "ATTACK"
    assert red.screening_allowed
    assert new_screening_allowed(red)


def test_budget_hit_with_no_fills_stops_screening_without_halt(tmp_path):
    _d, repo = db(tmp_path)
    s = _budget_settings()
    eng = RegimeEngine(s, repo)
    eng.note_ai_call(9)
    under = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert under.mode == "ATTACK"
    assert under.screening_allowed
    assert not repo.state().halted
    eng.note_ai_call(1)  # burn == 10
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("ai_session_budget")
    assert not st.screening_allowed
    assert not new_screening_allowed(st)
    assert eng.max_ai_calls() == 0
    assert not repo.state().halted
    assert not repo.cohort_has_taken_fills()


def test_post_fill_unrealized_below_burn_stops_screening_not_die(tmp_path):
    _d, repo = db(tmp_path)
    s = _budget_settings()
    repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 5, "price": 0.4})
    eng = RegimeEngine(s, repo)
    eng.note_ai_call(3)  # burn 3
    covered = eng.update(equity=50.0, unrealized_pnl=3.0)
    assert covered.has_taken_fills
    assert covered.mode != "DIE"
    assert covered.screening_allowed
    assert new_screening_allowed(covered)
    # Equal to burn still screens (strictly less-than).
    equal = eng.update(equity=50.0, unrealized_pnl=3.0)
    assert equal.screening_allowed
    stopped = eng.update(equity=50.0, unrealized_pnl=2.99)
    assert stopped.mode != "DIE"
    assert stopped.mode == "ATTACK"
    assert not stopped.screening_allowed
    assert stopped.reason.startswith("screening_stop")
    assert not new_screening_allowed(stopped)
    assert eng.max_ai_calls("ATTACK") == 0
    assert not repo.state().halted
    # Past the $10 cap, a covering unrealized book keeps screening.
    eng.note_ai_call(9)  # burn 12
    still = eng.update(equity=50.0, unrealized_pnl=12.0)
    assert still.session_ai_cost_usd == pytest.approx(12.0)
    assert still.mode != "DIE"
    assert still.screening_allowed


def test_open_position_without_fill_row_uses_post_fill_rule(tmp_path):
    _d, repo = db(tmp_path)
    repo.upsert_position(
        {
            "token_id": "yes1",
            "market_id": "m1",
            "shares": 10,
            "avg_price": 0.4,
            "realized_pnl": 0,
            "category": "crypto",
            "correlation_group": "m1",
        }
    )
    assert repo.cohort_has_taken_fills()
    eng = RegimeEngine(_budget_settings(), repo)
    eng.note_ai_call(10)
    st = eng.update(equity=50.0, unrealized_pnl=1.0)
    assert st.has_taken_fills
    assert st.mode != "DIE"
    assert not st.screening_allowed
    assert not repo.state().halted


def test_kill_floor_still_beats_session_budget(tmp_path):
    _d, repo = db(tmp_path)
    eng = RegimeEngine(_budget_settings(kill_floor_pct=0.20), repo)
    eng.note_ai_call(10)
    st = eng.update(equity=39.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("kill_floor")
    assert not repo.state().halted


def test_burn_and_budget_decision_persist_across_restart(tmp_path):
    _d, repo = db(tmp_path)
    s = _budget_settings()
    eng = RegimeEngine(s, repo)
    assert eng.update(equity=50.0, unrealized_pnl=0.0).screening_allowed
    eng.note_ai_call(10)
    died = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert died.mode == "DIE"
    assert died.reason.startswith("ai_session_budget")
    restored = RegimeEngine(s, repo)
    assert restored.session_ai_calls == 10
    assert restored.session_ai_cost_usd == pytest.approx(10.0)
    again = restored.update(equity=50.0, unrealized_pnl=0.0)
    assert again.mode == "DIE"
    assert not again.screening_allowed
    assert not repo.state().halted

    repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 1, "price": 0.5})
    post = RegimeEngine(s, repo)
    assert post.session_ai_calls == 10
    stopped = post.update(equity=50.0, unrealized_pnl=1.0)
    assert stopped.has_taken_fills
    assert stopped.mode != "DIE"
    assert not stopped.screening_allowed
    resumed = post.update(equity=50.0, unrealized_pnl=10.0)
    assert resumed.screening_allowed
    again_stopped = RegimeEngine(s, repo).update(equity=50.0, unrealized_pnl=1.0)
    assert not again_stopped.screening_allowed
    assert again_stopped.session_ai_calls == 10


def test_fill_signal_read_failure_is_closed():
    class Bad:
        def ai_burn_state(self):
            return None, 0

        def week_baseline_state(self):
            return None, None

        def cohort_has_taken_fills(self):
            raise DatabaseError("fills down")

    eng = RegimeEngine(_budget_settings(), Bad())
    with pytest.raises(DatabaseError):
        eng.update(equity=50.0, unrealized_pnl=0.0)


async def _drive(app: TradingApp) -> dict[str, int]:
    calls = {"review": 0, "scan": 0, "consider": 0}

    async def review(_marks):
        calls["review"] += 1

    async def scan():
        calls["scan"] += 1
        return [(market(), None)]

    async def consider(_m, _positions, _marks):
        calls["consider"] += 1
        return False

    app._review_holdings = review  # type: ignore[method-assign]
    app.scanner.scan = scan  # type: ignore[method-assign]
    app._consider = consider  # type: ignore[method-assign]
    await app.cycle()
    return calls


@pytest.mark.asyncio
async def test_budget_die_still_reviews_holdings_and_skips_screening(tmp_path):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
    )
    app = TradingApp(s)
    app.regime.note_ai_call(10)
    calls = await _drive(app)
    assert calls == {"review": 1, "scan": 0, "consider": 0}
    assert app.regime.last is not None
    assert app.regime.last.mode == "DIE"
    assert app.regime.last.reason.startswith("ai_session_budget")
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_post_fill_stop_still_reviews_holdings(tmp_path):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
    )
    app = TradingApp(s)
    app.repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 2, "price": 0.4})
    app.regime.note_ai_call(4)
    calls = await _drive(app)
    assert calls["review"] == 1
    assert calls["scan"] == 0
    assert calls["consider"] == 0
    assert app.regime.last is not None
    assert app.regime.last.mode != "DIE"
    assert not app.regime.last.screening_allowed
    assert app.regime.last.has_taken_fills
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_zero_burn_cycle_screens_and_scan_failure_still_reviews(tmp_path):
    s = settings(
        tmp_path,
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
    )
    app = TradingApp(s)
    calls = await _drive(app)
    assert calls == {"review": 1, "scan": 1, "consider": 1}
    assert app.regime.last is not None
    assert app.regime.last.mode == "ATTACK"
    assert app.regime.last.screening_allowed

    reviewed = {"n": 0}

    async def review(_marks):
        reviewed["n"] += 1

    async def boom():
        raise RuntimeError("gamma down")

    app._review_holdings = review  # type: ignore[method-assign]
    app.scanner.scan = boom  # type: ignore[method-assign]
    await app.cycle()
    assert reviewed["n"] == 1
    assert not app.repo.state().halted
