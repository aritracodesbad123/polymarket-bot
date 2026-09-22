"""Pre-AI book-mid band. Edges are tradeable; outside skips the model."""

import json

import pytest

from app.config import Settings
from app.main import TradingApp
from app.market_data.scanner import MID_OUTSIDE_BAND, filter_tradeable_mid
from tests.conftest import book, estimate, market, settings


def test_band_edges_are_tradeable_and_outside_skips():
    s = Settings()
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90
    assert filter_tradeable_mid(0.10, s) is None
    assert filter_tradeable_mid(0.90, s) is None
    assert filter_tradeable_mid(0.50, s) is None
    assert filter_tradeable_mid(0.05, s) == "mid_outside_band"
    assert filter_tradeable_mid(0.95, s) == MID_OUTSIDE_BAND
    assert filter_tradeable_mid(0.099999, s) == MID_OUTSIDE_BAND
    assert filter_tradeable_mid(0.900001, s) == MID_OUTSIDE_BAND
    assert filter_tradeable_mid(None, s) == MID_OUTSIDE_BAND


def test_custom_band_from_settings():
    s = Settings(min_tradeable_mid=0.20, max_tradeable_mid=0.80)
    assert filter_tradeable_mid(0.20, s) is None
    assert filter_tradeable_mid(0.80, s) is None
    assert filter_tradeable_mid(0.15, s) == MID_OUTSIDE_BAND
    assert filter_tradeable_mid(0.85, s) == MID_OUTSIDE_BAND


class _Engine:
    def __init__(self):
        self.calls = 0
        self.provider = "grok"
        self.last_model = "stub"
        self.grok = None
        self.gemini = None

    async def estimate(self, packet):
        self.calls += 1
        # Echo the mid so edge vs the ask stays negative and nothing fills.
        return estimate(p=float(packet.implied_probability or 0.5))


class _Research:
    def __init__(self):
        self.calls = 0
        self.blocked = False

    async def gather(self, packet):
        self.calls += 1
        return packet


def _complement(bid: float, ask: float):
    """NO book around 1 - YES mid, tight enough to pass the spread filter."""
    return book(token_id="no1", bid=round(1.0 - ask, 2), ask=round(1.0 - bid, 2))


async def _screen(tmp_path, bid: float, ask: float, **setting_kw) -> tuple[TradingApp, _Engine, _Research]:
    s = settings(tmp_path, **setting_kw)
    assert s.ai_session_budget_usd == 10.0
    app = TradingApp(s)
    eng = _Engine()
    research = _Research()
    app.engine = eng
    app.research = research
    yes = book(token_id="yes1", bid=bid, ask=ask)
    no = _complement(bid, ask)

    async def books(_m):
        return yes, no

    async def scan():
        return [(market(), None)]

    app._books = books  # type: ignore[method-assign]
    app.scanner.scan = scan  # type: ignore[method-assign]
    await app._screen_new_markets({})
    return app, eng, research


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bid,ask,expect_ai,expected_mid",
    [
        (0.04, 0.06, False, 0.05),
        (0.94, 0.96, False, 0.95),
        (0.49, 0.51, True, 0.50),
        (0.08, 0.12, True, 0.10),
        (0.88, 0.92, True, 0.90),
    ],
)
async def test_consider_skips_ai_outside_band(tmp_path, bid, ask, expect_ai, expected_mid):
    yes = book(token_id="yes1", bid=bid, ask=ask)
    assert yes.midpoint == pytest.approx(expected_mid)
    app, eng, research = await _screen(tmp_path, bid, ask)
    decisions = app.repo.db.query(
        "SELECT approved, reject_reason, gates_json FROM trade_decisions"
    )
    events = app.repo.db.query(
        "SELECT kind, message, payload_json FROM system_events WHERE kind='TRADE_REJECTED'"
    )
    preds = app.repo.db.query("SELECT id FROM ai_predictions")
    if expect_ai:
        assert eng.calls == 1
        assert research.calls == 1
        assert app.regime.session_ai_calls == 1
        assert all(d["reject_reason"] != MID_OUTSIDE_BAND for d in decisions)
        assert preds
    else:
        assert eng.calls == 0
        assert research.calls == 0
        assert app.regime.session_ai_calls == 0
        assert app.regime.session_ai_cost_usd == 0.0
        assert not preds
        assert len(decisions) == 1
        assert decisions[0]["approved"] == 0
        assert decisions[0]["reject_reason"] == MID_OUTSIDE_BAND
        gates = json.loads(decisions[0]["gates_json"])
        assert gates[MID_OUTSIDE_BAND]["passed"] is False
        assert f"{expected_mid:.4f}" in gates[MID_OUTSIDE_BAND]["detail"]
        assert len(events) == 1
        assert MID_OUTSIDE_BAND in events[0]["message"]
        payload = json.loads(events[0]["payload_json"])
        assert payload["mid"] == pytest.approx(expected_mid)
        assert payload["min_tradeable_mid"] == 0.10
        assert payload["max_tradeable_mid"] == 0.90


@pytest.mark.asyncio
async def test_custom_band_skips_before_ai(tmp_path):
    app, eng, research = await _screen(
        tmp_path,
        0.14,
        0.16,
        min_tradeable_mid=0.20,
        max_tradeable_mid=0.80,
    )
    assert book(bid=0.14, ask=0.16).midpoint == pytest.approx(0.15)
    assert eng.calls == 0
    assert research.calls == 0
    assert app.regime.session_ai_calls == 0
    row = app.repo.db.query_one("SELECT reject_reason FROM trade_decisions")
    assert row["reject_reason"] == MID_OUTSIDE_BAND
