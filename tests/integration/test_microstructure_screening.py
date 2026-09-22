"""AI budget stop keeps screening only when ESTIMATOR=microstructure."""

from __future__ import annotations

import json
import logging

import pytest

from app.ai.microstructure import (
    MICRO_COIN_FLIP_REJECT,
    MICRO_MIN_CONFIDENCE,
    MICRO_PROVIDER,
)
from app.main import GEMINI_KELLY_MULTIPLIER, TradingApp
from app.market_data.scanner import MID_OUTSIDE_BAND
from tests.conftest import book, estimate, market, settings


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


def _budget_settings(tmp_path, **kw):
    base = dict(
        api_die_cushion_usd=0.0,
        ai_session_budget_usd=10.0,
        estimated_usd_per_ai_call=1.0,
        paper_starting_bankroll=50.0,
        min_confidence_score=0.4,
    )
    base.update(kw)
    return settings(tmp_path, **base)


def _wire(
    app: TradingApp,
    *,
    bid: float = 0.38,
    ask: float = 0.40,
    bid_size: float = 8_000.0,
    ask_size: float = 2_000.0,
) -> dict[str, int]:
    calls = {"scan": 0}
    # |I| = 0.60 so Phase 1 does not reject micro_weak_imbalance before the
    # Survival edge gate. Equal top sizes are covered separately.
    yes = book(token_id="yes1", bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)
    no = book(
        token_id="no1",
        bid=round(1.0 - ask, 2),
        ask=round(1.0 - bid, 2),
        bid_size=ask_size,
        ask_size=bid_size,
    )

    async def scan():
        calls["scan"] += 1
        return [(market(), None)]

    async def books(_m):
        return yes, no

    app.scanner.scan = scan  # type: ignore[method-assign]
    app._books = books  # type: ignore[method-assign]
    return calls


async def _cycle(app: TradingApp, caplog) -> None:
    app.log.propagate = True
    with caplog.at_level(logging.INFO, logger="polygrok"):
        await app.cycle()


@pytest.mark.asyncio
async def test_budget_die_screens_via_micro_and_logs_provider(tmp_path, caplog):
    app = TradingApp(_budget_settings(tmp_path, estimator="microstructure"))
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    calls = _wire(app)
    app.regime.note_ai_call(10)
    await _cycle(app, caplog)

    assert calls["scan"] == 1
    assert eng.calls == 0
    assert research.calls == 0
    assert app.regime.session_ai_calls == 10
    assert app.regime.last is not None
    assert app.regime.last.mode == "DIE"
    assert app.regime.last.reason.startswith("ai_session_budget")
    assert "provider=micro" in caplog.text
    assert "gemini" not in caplog.text.lower()
    events = app.repo.db.query(
        "SELECT message, payload_json FROM system_events WHERE kind='AI_ESTIMATE'"
    )
    assert events
    assert events[0]["message"] == "provider=micro"
    payload = json.loads(events[0]["payload_json"])
    assert payload["provider"] == MICRO_PROVIDER
    assert payload["imbalance"] == pytest.approx(0.6)
    assert payload["lambda"] == pytest.approx(0.08)
    assert payload["microprice"] is not None
    preds = app.repo.db.query(
        "SELECT model, should_abstain, confidence_score FROM ai_predictions"
    )
    assert len(preds) == 1
    assert preds[0]["model"] == "micro"
    assert preds[0]["should_abstain"] == 0
    assert preds[0]["confidence_score"] >= MICRO_MIN_CONFIDENCE
    decisions = app.repo.db.query(
        "SELECT approved, reject_reason, gates_json FROM trade_decisions"
    )
    assert decisions
    assert all(d["approved"] == 0 for d in decisions)
    assert {d["reject_reason"] for d in decisions} == {"edge_too_small"}
    for row in decisions:
        gates = json.loads(row["gates_json"])
        assert gates["ai_provider"]["detail"] == "micro"
        assert "grok" not in gates["ai_provider"]["detail"]
        assert "gemini" not in gates["ai_provider"]["detail"]
    # Kelly 0.25 stays. The Gemini half-Kelly lock is not applied on this path.
    assert app.settings.kelly_multiplier == 0.25
    assert app.settings.max_spread == 0.06
    kw = app._strategy_kwargs()
    assert kw["min_confidence_score"] == 0.50
    assert "kelly_multiplier" not in kw
    assert GEMINI_KELLY_MULTIPLIER == 0.125
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_balanced_book_rejects_weak_imbalance(tmp_path):
    app = TradingApp(
        _budget_settings(
            tmp_path,
            estimator="microstructure",
            db_path=str(tmp_path / "balanced.db"),
        )
    )
    app.engine = _Engine()
    app.research = _Research()
    calls = _wire(app, bid_size=2_000, ask_size=2_000)
    app.regime.note_ai_call(10)
    await app.cycle()
    assert calls["scan"] == 1
    assert app.engine.calls == 0
    assert app.repo.db.query("SELECT id FROM ai_predictions") == []
    row = app.repo.db.query_one("SELECT reject_reason, gates_json FROM trade_decisions")
    assert row["reject_reason"] == "micro_weak_imbalance"
    gates = json.loads(row["gates_json"])
    assert gates["ai_provider"]["detail"] == "micro"
    event = app.repo.db.query_one(
        "SELECT payload_json FROM system_events WHERE kind='AI_ESTIMATE'"
    )
    payload = json.loads(event["payload_json"])
    assert payload["imbalance"] == pytest.approx(0.0)
    assert payload["microprice"] is not None
    assert payload["fair"] is not None


@pytest.mark.asyncio
async def test_coin_flip_mid_rejects_micro_entry(tmp_path):
    """Mid 0.50 on the micro path rejects as micro_coin_flip_mid; LLM not called."""
    app = TradingApp(
        _budget_settings(
            tmp_path,
            estimator="microstructure",
            db_path=str(tmp_path / "coin.db"),
        )
    )
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    # Strong |I|; would clear weak-imbalance. Mid sits in the coin-flip band.
    calls = _wire(app, bid=0.49, ask=0.51, bid_size=9_000, ask_size=1_000)
    assert book(bid=0.49, ask=0.51).midpoint == pytest.approx(0.50)
    app.regime.note_ai_call(10)
    await app.cycle()
    assert calls["scan"] == 1
    assert eng.calls == 0
    assert research.calls == 0
    assert app.repo.db.query("SELECT id FROM ai_predictions") == []
    row = app.repo.db.query_one("SELECT reject_reason, gates_json FROM trade_decisions")
    assert row["reject_reason"] == MICRO_COIN_FLIP_REJECT
    gates = json.loads(row["gates_json"])
    assert gates["ai_provider"]["detail"] == "micro"
    assert app.settings.kelly_multiplier == 0.25
    assert app.settings.min_edge == 0.05
    assert app.settings.max_spread == 0.06


@pytest.mark.asyncio
@pytest.mark.parametrize("bid,ask,mid", [(0.39, 0.41, 0.40), (0.59, 0.61, 0.60)])
async def test_micro_outside_coin_flip_proceeds_past_gate(tmp_path, bid, ask, mid):
    """Mid 0.40 / 0.60 clear micro_coin_flip_mid; other gates may still reject."""
    app = TradingApp(
        _budget_settings(
            tmp_path,
            estimator="microstructure",
            db_path=str(tmp_path / f"outside-{mid}.db"),
        )
    )
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app, bid=bid, ask=ask, bid_size=9_000, ask_size=1_000)
    assert book(bid=bid, ask=ask).midpoint == pytest.approx(mid)
    app.regime.note_ai_call(10)
    await app.cycle()
    assert calls["scan"] == 1
    assert eng.calls == 0
    decisions = app.repo.db.query("SELECT reject_reason, gates_json FROM trade_decisions")
    assert decisions
    assert all(d["reject_reason"] != MICRO_COIN_FLIP_REJECT for d in decisions)
    for row in decisions:
        gates = json.loads(row["gates_json"])
        assert gates["ai_provider"]["detail"] == "micro"


@pytest.mark.asyncio
async def test_llm_path_unaffected_by_coin_flip_mid(tmp_path):
    """Shared mid band still allows 0.50 on the Gemini/LLM path."""
    app = TradingApp(
        _budget_settings(
            tmp_path,
            estimator=None,
            db_path=str(tmp_path / "llm-coin.db"),
        )
    )
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    calls = _wire(app, bid=0.49, ask=0.51)
    assert book(bid=0.49, ask=0.51).midpoint == pytest.approx(0.50)
    await app.cycle()
    assert calls["scan"] == 1
    assert eng.calls == 1
    assert research.calls == 1
    assert app.regime.session_ai_calls == 1
    decisions = app.repo.db.query("SELECT reject_reason FROM trade_decisions")
    assert decisions
    assert all(d["reject_reason"] != "micro_coin_flip_mid" for d in decisions)


@pytest.mark.asyncio
async def test_post_fill_screening_stop_uses_micro(tmp_path, caplog):
    app = TradingApp(_budget_settings(tmp_path, estimator="microstructure"))
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app)
    app.repo.insert_fill({"token_id": "yes1", "side": "BUY", "shares": 2, "price": 0.4})
    app.regime.note_ai_call(4)
    await _cycle(app, caplog)

    assert calls["scan"] == 1
    assert eng.calls == 0
    assert app.regime.session_ai_calls == 4
    assert app.regime.last is not None
    assert app.regime.last.mode != "DIE"
    assert app.regime.last.has_taken_fills
    assert app.regime.last.reason.startswith("screening_stop")
    assert "provider=micro" in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds and preds[0]["model"] == "micro"
    assert not app.repo.state().halted


@pytest.mark.asyncio
@pytest.mark.parametrize("estimator", [None, "off", ""])
async def test_estimator_unset_preserves_burn_stop(tmp_path, estimator):
    app = TradingApp(_budget_settings(tmp_path, estimator=estimator))
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app)
    app.regime.note_ai_call(10)
    await app.cycle()
    assert calls["scan"] == 0
    assert eng.calls == 0
    assert app.regime.last is not None
    assert app.regime.last.mode == "DIE"
    assert not app.regime.last.screening_allowed
    assert app.repo.db.query("SELECT id FROM ai_predictions") == []
    assert not app.repo.state().halted


@pytest.mark.asyncio
async def test_estimator_flag_does_not_replace_llm_before_budget(tmp_path, caplog):
    app = TradingApp(_budget_settings(tmp_path, estimator="microstructure"))
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    calls = _wire(app)
    await _cycle(app, caplog)
    assert calls["scan"] == 1
    assert eng.calls == 1
    assert research.calls == 1
    assert app.regime.session_ai_calls == 1
    assert "provider=micro" not in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds
    assert preds[0]["model"] != "micro"
    assert "grok" in preds[0]["model"]


@pytest.mark.asyncio
async def test_force_microstructure_runs_without_budget_exhaustion(tmp_path, caplog):
    app = TradingApp(_budget_settings(tmp_path, estimator=None))
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    app.enable_microstructure()
    calls = _wire(app)
    await _cycle(app, caplog)
    assert calls["scan"] == 1
    assert eng.calls == 0
    assert research.calls == 0
    assert app.regime.session_ai_calls == 0
    assert app.regime.last is not None
    assert app.regime.last.mode == "ATTACK"
    assert "provider=micro" in caplog.text
    preds = app.repo.db.query("SELECT model FROM ai_predictions")
    assert preds and preds[0]["model"] == "micro"


@pytest.mark.asyncio
async def test_kill_floor_and_weekly_stop_stay_dark(tmp_path):
    kill_app = TradingApp(
        _budget_settings(tmp_path, estimator="microstructure", kill_floor_pct=0.20)
    )
    kill_app.engine = _Engine()
    kill_calls = _wire(kill_app)
    kill_app.paper.cash = 30.0
    await kill_app.cycle()
    assert kill_calls["scan"] == 0
    assert kill_app.regime.last is not None
    assert kill_app.regime.last.reason.startswith("kill_floor")
    assert kill_app.repo.db.query("SELECT id FROM ai_predictions") == []

    week_app = TradingApp(
        _budget_settings(
            tmp_path,
            estimator="microstructure",
            kill_floor_pct=0.50,
            weekly_loss_pct=0.05,
            db_path=str(tmp_path / "week.db"),
        )
    )
    week_app.engine = _Engine()
    week_calls = _wire(week_app)
    week_app.regime.update(equity=50.0, unrealized_pnl=0.0)
    week_app.paper.cash = 45.0
    await week_app.cycle()
    assert week_calls["scan"] == 0
    assert week_app.regime.last is not None
    assert week_app.regime.last.reason.startswith("weekly_equity_stop")
    assert week_app.repo.db.query("SELECT id FROM ai_predictions") == []


@pytest.mark.asyncio
async def test_micro_path_still_skips_mid_outside_band(tmp_path):
    app = TradingApp(_budget_settings(tmp_path, estimator="microstructure"))
    eng = _Engine()
    app.engine = eng
    app.research = _Research()
    calls = _wire(app, bid=0.04, ask=0.06)
    app.regime.note_ai_call(10)
    await app.cycle()
    assert calls["scan"] == 1
    assert eng.calls == 0
    assert app.regime.session_ai_calls == 10
    assert app.repo.db.query("SELECT id FROM ai_predictions") == []
    row = app.repo.db.query_one(
        "SELECT reject_reason, gates_json FROM trade_decisions"
    )
    assert row["reject_reason"] == MID_OUTSIDE_BAND
    gates = json.loads(row["gates_json"])
    assert gates["ai_provider"]["detail"] == "micro"
