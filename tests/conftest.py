from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ai.schemas import MarketEstimate
from app.config import Settings
from app.market_data.models import BookLevel, Market, OrderBook
from app.storage.db import Database
from app.storage.repositories import Repositories


def settings(tmp_path, **kwargs) -> Settings:
    kw = dict(
        db_path=str(tmp_path / "t.db"),
        xai_api_key=None,
        trading_mode="paper",
        live_trading_enabled=False,
    )
    kw.update(kwargs)
    return Settings(**kw)


def db(tmp_path) -> tuple[Database, Repositories]:
    d = Database(tmp_path / "t.db")
    return d, Repositories(d)


def market(**kwargs) -> Market:
    now = datetime.now(timezone.utc)
    base = dict(
        market_id="m1",
        condition_id="c1",
        yes_token_id="yes1",
        no_token_id="no1",
        question="Will X happen?",
        description="Resolves YES if X.",
        resolution_criteria="Official source.",
        close_time=now + timedelta(days=14),
        category="politics",
        event_id="e1",
        correlation_group="e1",
        liquidity=5000.0,
        volume=8000.0,
        active=True,
        closed=False,
        paused=False,
        status="active",
        min_order_size=1.0,
    )
    base.update(kwargs)
    return Market(**base)


def book(token_id="yes1", ask=0.40, bid=0.38, ask_size=2000.0, bid_size=2000.0, age_s=0.0) -> OrderBook:
    now = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return OrderBook(
        token_id=token_id,
        market_id="m1",
        bids=[BookLevel(price=bid, size=bid_size), BookLevel(price=bid - 0.02, size=bid_size)],
        asks=[
            BookLevel(price=ask, size=ask_size),
            BookLevel(price=ask + 0.02, size=ask_size),
        ],
        fetched_at=now,
    )


def estimate(p=0.70, abstain=False) -> MarketEstimate:
    return MarketEstimate(
        market_id="m1",
        estimated_probability=p,
        confidence="high",
        confidence_score=0.8,
        base_rate_probability=0.5,
        evidence_adjustment=0.2,
        key_evidence=["a"],
        counterarguments=["b"],
        uncertainty_factors=["c"],
        stale_information_risk="low",
        should_abstain=abstain,
        abstention_reason="none" if not abstain else "thin evidence",
        reasoning_summary="test",
    )
