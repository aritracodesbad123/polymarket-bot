"""Restart persistence, weekly stop, absolute caps, fail-closed."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.broker.paper import PaperBroker, PaperPosition
from app.cli import cmd_reset_week_baseline
from app.config import Settings
from app.main import (
    GEMINI_EDGE_TIGHTEN,
    GEMINI_KELLY_MULTIPLIER,
    GEMINI_MIN_CONFIDENCE,
    GEMINI_MIN_EXEC_EDGE,
    TradingApp,
)
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskManager
from app.risk.regime import RegimeEngine, iso_week_monday
from app.storage.db import Database, DatabaseError
from app.storage.repositories import Repositories
from app.strategy.evaluator import StrategyEvaluator
from tests.conftest import book, db, estimate, market, settings


def _settings(**kw) -> Settings:
    base = dict(
        db_path=":memory:",
        paper_starting_bankroll=50.0,
        estimated_usd_per_ai_call=0.02,
        api_die_cushion_usd=0.50,
        kill_floor_pct=0.20,
        regime_enabled=True,
        min_edge=0.05,
        max_spread=0.06,
        kelly_multiplier=0.25,
        defend_edge_tighten=0.02,
        defend_kelly_mult=0.5,
        defend_max_grok_calls=3,
        max_grok_calls_per_cycle=8,
    )
    base.update(kw)
    return Settings(**base)


def _clock(start: datetime):
    box = {"t": start}

    def now() -> datetime:
        return box["t"]

    return box, now


def test_gemini_survival_lock_constants_unchanged():
    assert GEMINI_EDGE_TIGHTEN == 0.02
    assert GEMINI_MIN_CONFIDENCE == 0.50
    assert GEMINI_MIN_EXEC_EDGE == 0.02
    assert GEMINI_KELLY_MULTIPLIER == 0.125


def test_week_boundary_is_monday_utc():
    assert iso_week_monday(date(2026, 9, 22)) == date(2026, 9, 21)
    assert iso_week_monday(date(2026, 9, 28)) == date(2026, 9, 28)


def test_burn_persists_across_restart_and_multiple_calls(tmp_path):
    _d, repo = db(tmp_path)
    box, now = _clock(datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    s = _settings(api_die_cushion_usd=0.0, estimated_usd_per_ai_call=0.02)
    eng = RegimeEngine(s, repo, now=now)
    assert eng.update(equity=50.0, unrealized_pnl=0.0).mode == "ATTACK"
    eng.note_ai_call(1)
    eng.note_ai_call(2)
    assert eng.session_ai_calls == 3
    assert eng.session_ai_cost_usd == pytest.approx(0.06)
    eng2 = RegimeEngine(s, repo, now=now)
    assert eng2.session_ai_calls == 3
    assert eng2.session_ai_cost_usd == pytest.approx(3 * s.estimated_usd_per_ai_call)
    assert eng2.update(equity=50.0, unrealized_pnl=0.0).mode == "DIE"
    box["t"] = datetime(2026, 9, 23, 0, 5, tzinfo=timezone.utc)
    eng3 = RegimeEngine(s, repo, now=now)
    assert eng3.session_ai_calls == 0
    assert eng3.update(equity=50.0, unrealized_pnl=0.0).mode == "ATTACK"
    # Rollover was persisted, not only in memory.
    eng4 = RegimeEngine(s, repo, now=now)
    assert eng4.session_ai_calls == 0


def test_same_process_day_rollover_clears_burn(tmp_path):
    _d, repo = db(tmp_path)
    box, now = _clock(datetime(2026, 9, 22, 23, tzinfo=timezone.utc))
    s = _settings(api_die_cushion_usd=0.0)
    eng = RegimeEngine(s, repo, now=now)
    eng.note_ai_call(1)
    assert eng.update(equity=50.0, unrealized_pnl=0.0).mode == "DIE"
    box["t"] = datetime(2026, 9, 23, 0, 1, tzinfo=timezone.utc)
    st = eng.update(equity=50.0, unrealized_pnl=0.0)
    assert eng.session_ai_calls == 0
    assert st.mode == "ATTACK"


def test_zero_burn_then_first_paid_call_persisted(tmp_path):
    _d, repo = db(tmp_path)
    s = _settings(api_die_cushion_usd=0.0, estimated_usd_per_ai_call=0.02)
    eng = RegimeEngine(s, repo)
    assert eng.update(equity=50.0, unrealized_pnl=0.0).mode == "ATTACK"
    eng.note_ai_call(1)
    died = RegimeEngine(s, repo)
    assert died.session_ai_calls == 1
    assert died.update(equity=50.0, unrealized_pnl=0.0).mode == "DIE"


def test_burn_read_failure_closed():
    class Bad:
        def ai_burn_state(self):
            raise DatabaseError("read failed")

        def week_baseline_state(self):
            return None, None

    with pytest.raises(DatabaseError):
        RegimeEngine(_settings(), Bad())


def test_burn_write_failure_halts_and_raises(tmp_path):
    _d, repo = db(tmp_path)

    class WriteFail:
        def ai_burn_state(self):
            return repo.ai_burn_state()

        def week_baseline_state(self):
            return repo.week_baseline_state()

        def set_ai_burn(self, day, count):
            raise DatabaseError("disk")

        def state(self):
            return repo.state()

        def halt(self, reason):
            return repo.halt(reason)

    eng = RegimeEngine(_settings(), WriteFail())
    with pytest.raises(DatabaseError):
        eng.note_ai_call(1)
    assert repo.state().halted
    assert repo.state().halt_reason == "ai_burn_persist_failed"


def test_inconsistent_week_baseline_fails_closed(tmp_path):
    d, repo = db(tmp_path)
    d.execute(
        "UPDATE system_state SET week_started_on='2026-09-21' WHERE id=1"
    )
    with pytest.raises(DatabaseError):
        RegimeEngine(_settings(), repo)


def test_corrupt_burn_fails_closed(tmp_path):
    d, repo = db(tmp_path)
    d.execute("UPDATE system_state SET ai_call_count=4 WHERE id=1")
    with pytest.raises(DatabaseError):
        RegimeEngine(_settings(), repo)


def test_closed_db_burn_write_fails_closed(tmp_path):
    d, repo = db(tmp_path)
    eng = RegimeEngine(_settings(), repo)
    d.close()
    with pytest.raises(DatabaseError):
        eng.note_ai_call(1)


def test_legacy_system_state_migrates_then_persists_burn(tmp_path):
    path = tmp_path / "old.db"
    cx = sqlite3.connect(path)
    cx.execute(
        """CREATE TABLE system_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paper_trading_started_at TEXT,
            halted INTEGER NOT NULL DEFAULT 0,
            halt_reason TEXT,
            trading_mode TEXT NOT NULL DEFAULT 'paper',
            live_activated_at TEXT,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    cx.execute(
        """INSERT INTO system_state
           (id, halted, trading_mode, created_at, updated_at)
           VALUES (1, 0, 'paper', 't', 't')"""
    )
    cx.commit()
    cx.close()
    d = Database(path)
    repo = Repositories(d)
    cols = {r[1] for r in d.query("PRAGMA table_info(system_state)")}
    assert "ai_call_count" in cols
    assert "week_baseline_equity" in cols
    assert "daily_realized_pnl" in cols
    s = _settings()
    eng = RegimeEngine(s, repo)
    eng.note_ai_call(2)
    d.close()
    eng2 = RegimeEngine(s, Repositories(Database(path)))
    assert eng2.session_ai_calls == 2


def test_weekly_stop_distinct_from_kill_floor_and_persists(tmp_path):
    _d, repo = db(tmp_path)
    box, now = _clock(datetime(2026, 9, 22, 15, tzinfo=timezone.utc))
    s = _settings(
        paper_starting_bankroll=5000.0,
        weekly_loss_pct=0.05,
        kill_floor_pct=0.20,
        api_die_cushion_usd=0.50,
    )
    eng = RegimeEngine(s, repo, now=now)
    assert eng.update(equity=5000.0, unrealized_pnl=0.0).mode == "ATTACK"
    assert repo.week_baseline_state() == ("2026-09-21", pytest.approx(5000.0))
    # 5% weekly floor is 4750. 20% kill floor is 4000. 4800 is neither.
    assert eng.update(equity=4800.0, unrealized_pnl=0.0).mode != "DIE"
    st = eng.update(equity=4750.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("weekly_equity_stop")
    assert "kill_floor" not in st.reason
    assert repo.state().halted
    assert repo.state().halt_reason.startswith("weekly_equity_stop")
    eng2 = RegimeEngine(s, repo, now=now)
    assert eng2.update(equity=4750.0, unrealized_pnl=0.0).mode == "DIE"
    assert repo.week_baseline_state()[1] == pytest.approx(5000.0)


def test_cohort_kill_floor_4500_and_weekly_stop_4750(tmp_path):
    """$5,000 cohort lines: KILL_FLOOR_PCT=0.10 at 4500, weekly stop at 4750."""
    _d, repo = db(tmp_path)
    s = _settings(
        paper_starting_bankroll=5000.0,
        kill_floor_pct=0.10,
        weekly_loss_pct=0.05,
        api_die_cushion_usd=0.0,
    )
    eng = RegimeEngine(s, repo)
    assert eng.update(equity=5000.0, unrealized_pnl=0.0).mode == "ATTACK"
    assert eng.update(equity=4800.0, unrealized_pnl=0.0).mode != "DIE"
    weekly = eng.update(equity=4750.0, unrealized_pnl=0.0)
    assert weekly.mode == "DIE"
    assert weekly.reason.startswith("weekly_equity_stop")
    assert "kill_floor" not in weekly.reason
    assert repo.state().halt_reason.startswith("weekly_equity_stop")
    catastrophic = eng.update(equity=4500.0, unrealized_pnl=0.0)
    assert catastrophic.mode == "DIE"
    assert catastrophic.reason.startswith("kill_floor")
    assert "4500.00" in catastrophic.reason


def test_kill_floor_still_fires_and_is_not_a_halt_without_weekly(tmp_path):
    _d, repo = db(tmp_path)
    eng = RegimeEngine(_settings(), repo)
    st = eng.update(equity=39.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("kill_floor")
    assert not repo.state().halted


def test_deeper_loss_keeps_kill_floor_reason_and_weekly_halt(tmp_path):
    _d, repo = db(tmp_path)
    s = _settings(paper_starting_bankroll=5000.0, weekly_loss_pct=0.05, kill_floor_pct=0.20)
    eng = RegimeEngine(s, repo)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    st = eng.update(equity=3900.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("kill_floor")
    assert repo.state().halted
    assert repo.state().halt_reason.startswith("weekly_equity_stop")


def test_week_boundary_resets_baseline_not_halt(tmp_path):
    _d, repo = db(tmp_path)
    box, now = _clock(datetime(2026, 9, 22, 15, tzinfo=timezone.utc))
    s = _settings(paper_starting_bankroll=5000.0, weekly_loss_pct=0.05)
    eng = RegimeEngine(s, repo, now=now)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    eng.update(equity=4750.0, unrealized_pnl=0.0)
    assert repo.state().halted
    reason = repo.state().halt_reason
    box["t"] = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)
    st = eng.update(equity=4750.0, unrealized_pnl=0.0)
    assert repo.week_baseline_state()[0] == "2026-09-28"
    assert repo.week_baseline_state()[1] == pytest.approx(4750.0)
    assert st.mode != "DIE"
    assert repo.state().halted
    assert repo.state().halt_reason == reason


def test_resume_does_not_reset_week_and_same_week_retrips(tmp_path):
    _d, repo = db(tmp_path)
    _box, now = _clock(datetime(2026, 9, 22, 15, tzinfo=timezone.utc))
    s = _settings(paper_starting_bankroll=5000.0, weekly_loss_pct=0.05)
    eng = RegimeEngine(s, repo, now=now)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    eng.update(equity=4750.0, unrealized_pnl=0.0)
    repo.resume_paper()
    assert not repo.state().halted
    assert repo.week_baseline_state()[1] == pytest.approx(5000.0)
    eng2 = RegimeEngine(s, repo, now=now)
    assert eng2.update(equity=4750.0, unrealized_pnl=0.0).mode == "DIE"
    assert repo.state().halted


def test_intentional_week_reset(tmp_path):
    _d, repo = db(tmp_path)
    s = _settings(paper_starting_bankroll=5000.0, weekly_loss_pct=0.05)
    eng = RegimeEngine(s, repo)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    repo.halt("weekly_equity_stop")
    eng.reset_week_baseline(4700.0)
    assert repo.state().halted
    assert repo.week_baseline_state()[1] == pytest.approx(4700.0)
    st = eng.update(equity=4700.0, unrealized_pnl=0.0)
    assert st.mode != "DIE"


def test_weekly_stop_when_regime_disabled(tmp_path):
    _d, repo = db(tmp_path)
    s = _settings(
        paper_starting_bankroll=5000.0,
        weekly_loss_pct=0.05,
        regime_enabled=False,
    )
    eng = RegimeEngine(s, repo)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    st = eng.update(equity=4700.0, unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("weekly_equity_stop")
    assert repo.state().halted


def test_hydrated_equity_trips_persisted_weekly_stop(tmp_path):
    _d, repo = db(tmp_path)
    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        weekly_loss_pct=0.05,
        kill_floor_pct=0.20,
    )
    box, now = _clock(datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    eng = RegimeEngine(s, repo, now=now)
    eng.update(equity=5000.0, unrealized_pnl=0.0)
    paper = PaperBroker(5000.0, 0)
    paper.cash = 4700.0
    Portfolio(paper, repo).snapshot({})
    restored = PaperBroker(5000.0, 0)
    Portfolio(restored, repo).hydrate_paper(5000.0)
    assert restored.cash == pytest.approx(4700.0)
    assert restored.equity() == pytest.approx(4700.0)
    eng2 = RegimeEngine(s, repo, now=now)
    st = eng2.update(equity=restored.equity(), unrealized_pnl=0.0)
    assert st.mode == "DIE"
    assert st.reason.startswith("weekly_equity_stop")
    _ = box


def test_reset_week_cli_does_not_clear_halt(tmp_path):
    app = TradingApp(
        settings(tmp_path, paper_starting_bankroll=5000.0, weekly_loss_pct=0.05)
    )
    app.paper.cash = 4800.0
    assert cmd_reset_week_baseline(app) == 0
    assert app.repo.week_baseline_state()[1] == pytest.approx(4800.0)
    app.repo.halt("weekly_equity_stop")
    assert cmd_reset_week_baseline(app) == 0
    assert app.repo.state().halted
    assert app.repo.week_baseline_state()[1] == pytest.approx(4800.0)


def _decide(s: Settings, **kw):
    args = dict(
        market=market(),
        book=book(),
        estimate=estimate(0.70),
        packet=object(),
        bankroll=s.paper_starting_bankroll,
        cash=s.paper_starting_bankroll,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
    )
    args.update(kw)
    return StrategyEvaluator(s).evaluate(**args)


def test_absolute_position_cap_stricter_than_pct(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        max_position_pct_bankroll=0.03,
        max_position_usd=25.0,
        max_total_exposure_pct=0.25,
        max_total_exposure_usd=500.0,
    )
    d = _decide(s)
    assert d.approved
    assert d.size_usd <= 25.0 + 1e-6
    assert d.size_usd > 20.0


def test_pct_position_cap_stricter_than_absolute(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=100.0,
        max_position_pct_bankroll=0.03,
        max_position_usd=25.0,
    )
    d = _decide(s)
    assert d.approved
    assert d.size_usd <= 3.0 + 1e-6
    assert d.size_usd > 2.0


def test_unset_absolute_caps_keep_percentage_behavior(tmp_path):
    s = settings(tmp_path, paper_starting_bankroll=1000.0)
    assert s.max_position_usd is None
    assert s.max_total_exposure_usd is None
    d = _decide(s)
    assert d.approved
    # 3% of 1000 = 30. Kelly wants more, so the percentage cap binds above $25.
    assert d.size_usd <= 30.0 + 1e-6
    assert d.size_usd > 25.0


def test_absolute_exposure_cap_stricter_than_pct(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        max_position_usd=25.0,
        max_total_exposure_usd=500.0,
        max_total_exposure_pct=0.25,
    )
    d = _decide(s, existing_total_exposure=490.0)
    assert d.approved
    assert d.size_usd <= 10.0 + 1e-6
    blocked = _decide(s, existing_total_exposure=500.0)
    assert not blocked.approved


def test_pct_exposure_cap_stricter_than_absolute(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=1000.0,
        max_total_exposure_pct=0.25,
        max_total_exposure_usd=500.0,
        max_position_usd=100.0,
    )
    d = _decide(s, existing_total_exposure=240.0)
    assert d.approved
    assert d.size_usd <= 10.0 + 1e-6


def test_order_block_reason_uses_stricter_cap(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        max_position_usd=25.0,
        max_total_exposure_usd=500.0,
    )
    _d, repo = db(tmp_path)
    rm = RiskManager(s, repo)
    assert (
        rm.order_block_reason(size_usd=26, existing_total_exposure=0, bankroll=5000)
        == "position_usd_cap"
    )
    assert (
        rm.order_block_reason(size_usd=20, existing_total_exposure=490, bankroll=5000)
        == "exposure_usd_cap"
    )
    assert (
        rm.order_block_reason(size_usd=10, existing_total_exposure=0, bankroll=5000)
        is None
    )


def test_daily_realized_loss_persists_and_uses_stricter_cap(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        max_daily_loss_pct=0.05,
        max_daily_loss_usd=50.0,
    )
    _d, repo = db(tmp_path)
    box, now = _clock(datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    rm = RiskManager(s, repo, now=now)
    rm.note_realized_pnl(-49.0)
    assert not repo.state().halted
    rm2 = RiskManager(s, repo, now=now)
    assert rm2.daily_realized_pnl == pytest.approx(-49.0)
    assert not repo.state().halted
    rm2.note_realized_pnl(-1.0)
    assert repo.state().halted
    assert repo.state().halt_reason.startswith("daily_realized_loss_cap")
    rm3 = RiskManager(s, repo, now=now)
    assert rm3.daily_realized_pnl == pytest.approx(-50.0)
    assert repo.state().halted
    # New UTC day resets the counter but does not clear the halt.
    box["t"] = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)
    rm4 = RiskManager(s, repo, now=now)
    assert rm4.daily_realized_pnl == pytest.approx(0.0)
    assert repo.state().halted


def test_percentage_daily_cap_stricter_than_absolute(tmp_path):
    s = settings(
        tmp_path,
        paper_starting_bankroll=200.0,
        max_daily_loss_pct=0.05,
        max_daily_loss_usd=50.0,
    )
    _d, repo = db(tmp_path)
    rm = RiskManager(s, repo)
    rm.note_realized_pnl(-9.0)
    assert not repo.state().halted
    rm.note_realized_pnl(-1.0)
    assert repo.state().halted


def test_unset_daily_absolute_does_not_replace_percentage_kill(tmp_path):
    s = settings(tmp_path, paper_starting_bankroll=1000.0, max_daily_loss_pct=0.05)
    assert s.max_daily_loss_usd is None
    _d, repo = db(tmp_path)
    rm = RiskManager(s, repo)
    rm.note_realized_pnl(-100.0)
    assert not repo.state().halted
    rm2 = RiskManager(s, repo)
    assert rm2.daily_realized_pnl == pytest.approx(-100.0)
    rm2.daily_pnl = -60.0
    rm2.note_equity(940.0, 1000.0)
    assert repo.state().halt_reason == "daily_loss_limit"


def test_daily_realized_write_failure_closed(tmp_path):
    s = settings(tmp_path, max_daily_loss_usd=50.0, paper_starting_bankroll=5000.0)
    d, repo = db(tmp_path)
    rm = RiskManager(s, repo)
    d.close()
    with pytest.raises(DatabaseError):
        rm.note_realized_pnl(-1.0)


@pytest.mark.asyncio
async def test_executor_enforces_absolute_caps(tmp_path):
    from app.broker.live import LiveBroker
    from app.execution.executor import Executor
    from app.market_data.client import PolymarketClient
    from app.strategy.evaluator import Decision

    s = settings(
        tmp_path,
        paper_starting_bankroll=5000.0,
        max_position_usd=25.0,
        max_total_exposure_usd=500.0,
        max_position_pct_bankroll=0.03,
        max_total_exposure_pct=0.25,
    )
    _d, repo = db(tmp_path)
    paper = PaperBroker(5000.0, 0)
    live = LiveBroker(s, repo)
    data = PolymarketClient(s.polymarket_gamma_url, s.polymarket_api_url, s.polymarket_ws_url)

    async def fake_book(*_a, **_k):
        return book()

    data.get_order_book = fake_book  # type: ignore
    ex = Executor(s, repo, paper, live, data)
    oversized = Decision(
        approved=True,
        reject_reason=None,
        gates=[],
        market_id="m1",
        token_id="yes1",
        side="BUY",
        limit_price=0.40,
        market_price=0.40,
        size_shares=100.0,
        size_usd=40.0,
    )
    err = await ex.execute(oversized, 1)
    assert err == "position_usd_cap"
    paper._positions["yes-open"] = PaperPosition(
        token_id="yes-open",
        market_id="m-open",
        shares=490.0,
        avg_price=1.0,
    )
    near_cap = Decision(
        approved=True,
        reject_reason=None,
        gates=[],
        market_id="m2",
        token_id="yes2",
        side="BUY",
        limit_price=0.40,
        market_price=0.40,
        size_shares=50.0,
        size_usd=20.0,
    )
    err = await ex.execute(near_cap, 2)
    assert err == "exposure_usd_cap"


def test_optional_caps_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("KILL_FLOOR_PCT", raising=False)
    monkeypatch.setenv("MAX_POSITION_USD", "25")
    monkeypatch.setenv("MAX_TOTAL_EXPOSURE_USD", "500")
    monkeypatch.setenv("MAX_DAILY_LOSS_USD", "50")
    monkeypatch.setenv("WEEKLY_LOSS_PCT", "0.05")
    monkeypatch.setenv("API_DIE_CUSHION_USD", "0")
    monkeypatch.setenv("PAPER_STARTING_BANKROLL", "5000")
    s = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert s.max_position_usd == 25
    assert s.max_total_exposure_usd == 500
    assert s.max_daily_loss_usd == 50
    assert s.weekly_loss_pct == pytest.approx(0.05)
    assert s.api_die_cushion_usd == 0
    assert s.paper_starting_bankroll == 5000
    assert s.min_edge == 0.05
    assert s.max_spread == 0.06
    assert s.estimated_usd_per_ai_call == 0.02
    assert s.kill_floor_pct == 0.20
