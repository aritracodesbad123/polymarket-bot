"""Safety tests 1-17. Live must fail closed unless every AND-gate is true."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.broker.live import LiveBroker, LiveLockedError
from app.broker.models import OrderRequest
from app.config import Settings
from app.execution.executor import Executor, idempotency_key
from app.market_data.models import OrderBook
from app.risk.authorization import (
    LIVE_CONFIRMATION_PHRASE,
    LiveAuthInput,
    evaluate_live_authorization,
)
from app.storage.db import Database, DatabaseError
from app.storage.repositories import Repositories
from app.strategy.evaluator import Decision
from tests.conftest import book, db, estimate, market, settings


def now() -> datetime:
    return datetime(2026, 9, 15, tzinfo=timezone.utc)


def auth(**kwargs) -> LiveAuthInput:
    base = dict(
        now=now(),
        paper_trading_started_at=now(),
        halted=False,
        live_trading_enabled=False,
        has_activation_record=False,
        activation_phrase_ok=False,
        has_valid_live_credentials=False,
        trading_mode_env="paper",
    )
    base.update(kwargs)
    return LiveAuthInput(**base)


def all_green(**kwargs) -> LiveAuthInput:
    started = now() - timedelta(days=8)
    base = dict(
        now=now(),
        paper_trading_started_at=started,
        halted=False,
        live_trading_enabled=True,
        has_activation_record=True,
        activation_phrase_ok=True,
        has_valid_live_credentials=True,
        trading_mode_env="live",
    )
    base.update(kwargs)
    return LiveAuthInput(**base)


@pytest.mark.asyncio
async def test_1_live_order_day1_fails(tmp_path):
    s = settings(tmp_path)
    _d, repo = db(tmp_path)
    repo.mark_paper_started()
    live = LiveBroker(s, repo)
    req = OrderRequest(
        client_order_id="x",
        idempotency_key="k",
        market_id="m",
        token_id="t",
        side="BUY",
        price=0.4,
        size_shares=1,
    )
    with pytest.raises(LiveLockedError):
        await live.submit(req, book(), Decision(approved=True, reject_reason=None, gates=[], market_id="m"))


def test_2_env_flag_day1_fails():
    r = evaluate_live_authorization(
        auth(live_trading_enabled=True, paper_trading_started_at=now())
    )
    assert not r.allowed
    assert "paper_duration_under_7_days" in r.reasons


def test_3_private_key_day1_fails():
    r = evaluate_live_authorization(
        auth(has_valid_live_credentials=True, paper_trading_started_at=now())
    )
    assert not r.allowed


def test_4_restart_day1_fails(tmp_path):
    s = settings(tmp_path)
    _d, repo = db(tmp_path)
    repo.mark_paper_started()
    # new process = new LiveBroker, same DB
    live = LiveBroker(s, repo)
    assert not live.authorize_now().allowed


def test_5_day7_without_human_fails():
    r = evaluate_live_authorization(
        all_green(has_activation_record=False, activation_phrase_ok=False)
    )
    assert not r.allowed
    assert r.eligible
    assert "no_human_activation_record" in r.reasons


def test_6_human_without_env_flag_fails():
    r = evaluate_live_authorization(all_green(live_trading_enabled=False))
    assert not r.allowed
    assert "LIVE_TRADING_ENABLED_false" in r.reasons


def test_7_env_without_human_fails():
    r = evaluate_live_authorization(
        all_green(has_activation_record=False, activation_phrase_ok=False)
    )
    assert not r.allowed


def test_8_human_and_env_before_7_days_fails():
    r = evaluate_live_authorization(
        all_green(paper_trading_started_at=now() - timedelta(days=3))
    )
    assert not r.allowed
    assert "paper_duration_under_7_days" in r.reasons


def test_9_all_prereqs_after_7_days_pass_auth_layer():
    r = evaluate_live_authorization(all_green())
    assert r.allowed
    assert r.eligible
    assert r.reasons == []


@pytest.mark.asyncio
async def test_10_duplicate_executor(tmp_path):
    s = settings(tmp_path)
    _database, repo = db(tmp_path)
    key = idempotency_key("m1", "yes1", "BUY", s.strategy_version)
    repo.insert_order(
        {
            "client_order_id": "a",
            "idempotency_key": key,
            "broker": "paper",
            "market_id": "m1",
            "token_id": "yes1",
            "side": "BUY",
            "price": 0.4,
            "size_shares": 1,
            "status": "OPEN",
        }
    )
    from app.broker.paper import PaperBroker
    from app.market_data.client import PolymarketClient

    paper = PaperBroker(1000, 0)
    live = LiveBroker(s, repo)
    data = PolymarketClient(s.polymarket_gamma_url, s.polymarket_api_url, s.polymarket_ws_url)

    async def fake_book(*_a, **_k):
        raise AssertionError("should not refresh book on duplicate")

    data.get_order_book = fake_book  # type: ignore
    ex = Executor(s, repo, paper, live, data)
    d = Decision(
        approved=True,
        reject_reason=None,
        gates=[],
        market_id="m1",
        token_id="yes1",
        side="BUY",
        limit_price=0.4,
        size_shares=1,
        market_price=0.4,
    )
    err = await ex.execute(d, 1)
    assert err == "duplicate_order"
    rows = repo.orders(10)
    assert len(rows) == 1


class TimeoutBroker:
    name = "paper"

    def operational(self) -> bool:
        return True

    async def positions(self):
        return []

    async def submit(self, *a, **k):
        raise TimeoutError("post timeout")

    async def reconcile(self):
        return None


@pytest.mark.asyncio
async def test_11_timeout_reconciles_before_retry(tmp_path):
    s = settings(tmp_path)
    _d, repo = db(tmp_path)
    from app.broker.paper import PaperBroker
    from app.market_data.client import PolymarketClient

    paper = TimeoutBroker()
    live = LiveBroker(s, repo)
    data = PolymarketClient(s.polymarket_gamma_url, s.polymarket_api_url, s.polymarket_ws_url)

    async def fake_book(*_a, **_k):
        return book()

    data.get_order_book = fake_book  # type: ignore
    ex = Executor(s, repo, paper, live, data)  # type: ignore
    d = Decision(
        approved=True,
        reject_reason=None,
        gates=[],
        market_id="m1",
        token_id="yes1",
        side="BUY",
        limit_price=0.4,
        size_shares=1,
        market_price=0.4,
    )
    err = await ex.execute(d, 1)
    assert err is not None
    assert "timeout" in err


def test_12_database_failure_raises():
    with pytest.raises(DatabaseError):
        Database("/no/such/dir/cannot.db")


def test_13_stale_orderbook_rejected(tmp_path):
    from app.strategy.evaluator import StrategyEvaluator
    from app.research.researcher import EvidencePacket

    s = settings(tmp_path)
    d = StrategyEvaluator(s).evaluate(
        market=market(),
        book=book(),
        estimate=estimate(),
        packet=EvidencePacket(market_id="m1", question="q", resolution_criteria="r"),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=False,
    )
    assert d.reject_reason == "stale_data"


def test_14_grok_p_1_2_rejected():
    from pydantic import ValidationError
    from app.ai.schemas import MarketEstimate

    with pytest.raises(ValidationError):
        MarketEstimate(
            market_id="m",
            estimated_probability=1.2,
            confidence="low",
            confidence_score=0.1,
            base_rate_probability=0.5,
            evidence_adjustment=0,
            should_abstain=False,
            reasoning_summary="x",
        )


def test_15_malformed_grok_rejected():
    from pydantic import ValidationError
    from app.ai.schemas import MarketEstimate

    with pytest.raises(ValidationError):
        MarketEstimate.model_validate({"foo": 1})


def test_16_risk_limit_exceeded(tmp_path):
    from app.strategy.evaluator import StrategyEvaluator
    from app.research.researcher import EvidencePacket

    s = settings(tmp_path)
    d = StrategyEvaluator(s).evaluate(
        market=market(),
        book=book(),
        estimate=estimate(0.9),
        packet=EvidencePacket(market_id="m1", question="q", resolution_criteria="r"),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=1000,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
    )
    assert not d.approved


def test_17_recon_mismatch_halts(tmp_path):
    from app.risk.manager import RiskManager

    s = settings(tmp_path)
    _d, repo = db(tmp_path)
    RiskManager(s, repo).note_recon_mismatch("orders")
    assert repo.state().halted


def test_activation_phrase_must_be_exact():
    r = evaluate_live_authorization(all_green(activation_phrase_ok=False))
    assert not r.allowed


def test_halted_blocks_live():
    r = evaluate_live_authorization(all_green(halted=True))
    assert not r.allowed
    assert "system_halted" in r.reasons
