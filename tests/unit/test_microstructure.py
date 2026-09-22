"""Locked microstructure quotes, fee-aware edge, confidence, fail-closed size."""

from __future__ import annotations

import pytest

from app.ai.microstructure import (
    MICRO_MIN_CONFIDENCE,
    MicrostructureEstimator,
    MicrostructureReject,
    buy_yes_edge,
    cap_fair_to_half_spread,
    confidence_proxy,
    depth_score,
    fair_value,
    fee_hides_raw_edge,
    imbalance,
    imbalance_stability,
    microprice,
    select_side,
    sell_no_edge,
    spread_score,
)
from app.main import GEMINI_KELLY_MULTIPLIER, GEMINI_MIN_CONFIDENCE
from app.market_data.models import BookLevel, OrderBook
from app.risk.regime import RegimeState, ai_spend_blocked_screening
from app.strategy.evaluator import StrategyEvaluator, fee_per_share
from tests.conftest import book, market, settings


def test_microprice_and_imbalance():
    bid, ask, bid_size, ask_size = 0.40, 0.46, 300.0, 100.0
    assert microprice(bid, ask, bid_size, ask_size) == pytest.approx(
        (300 * 0.46 + 100 * 0.40) / 400
    )
    assert imbalance(bid_size, ask_size) == pytest.approx(0.5)
    assert imbalance(10, 0) == 1.0
    assert imbalance(0, 10) == -1.0
    assert imbalance(4, 4) == 0.0
    # One-sided size: microprice collapses to the touched price.
    assert microprice(0.40, 0.50, 10, 0) == pytest.approx(0.50)
    assert microprice(0.40, 0.50, 0, 10) == pytest.approx(0.40)


def test_zero_size_fails_closed_without_dividing():
    with pytest.raises(MicrostructureReject) as exc:
        microprice(0.40, 0.50, 0, 0)
    assert exc.value.reason == "micro_zero_size"
    with pytest.raises(MicrostructureReject) as exc2:
        imbalance(0, 0)
    assert exc2.value.reason == "micro_zero_size"
    with pytest.raises(MicrostructureReject):
        imbalance(-1, 0)
    with pytest.raises(MicrostructureReject):
        fair_value(0.40, 0.50, 0, 0)

    empty = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.40, size=0)],
        asks=[BookLevel(price=0.42, size=0)],
    )
    est = MicrostructureEstimator()
    result = est.estimate(empty, market_id="m1", category="politics")
    assert result.reject_reason == "micro_zero_size"
    assert result.estimate is None
    assert result.fair is None
    assert est.snapshots("m1") == []


def test_fair_matches_microprice_and_caps_half_spread():
    bid, ask, bid_size, ask_size = 0.40, 0.46, 300.0, 100.0
    fair = fair_value(bid, ask, bid_size, ask_size)
    mid = 0.43
    # I = 0.5, fair = 0.43 + 0.5 * 0.5 * 0.06 = 0.445
    assert fair == pytest.approx(0.445)
    assert fair == pytest.approx(microprice(bid, ask, bid_size, ask_size))
    assert abs(fair - mid) <= (ask - bid) / 2 + 1e-12

    # Cap binds for a raw fair outside the half-spread, and at I = ±1 the
    # locked formula sits on the cap (fair == ask or fair == bid).
    assert cap_fair_to_half_spread(0.90, 0.50, 0.03) == pytest.approx(0.53)
    assert cap_fair_to_half_spread(0.10, 0.50, 0.03) == pytest.approx(0.47)
    assert cap_fair_to_half_spread(0.51, 0.50, 0.03) == pytest.approx(0.51)
    hi = fair_value(0.40, 0.50, 5, 0)
    lo = fair_value(0.40, 0.50, 0, 5)
    assert hi == pytest.approx(0.50)
    assert lo == pytest.approx(0.40)
    assert abs(hi - 0.45) <= 0.05 + 1e-12
    assert abs(lo - 0.45) <= 0.05 + 1e-12


def test_crossed_or_missing_quote_fails_closed():
    crossed = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.50, size=10)],
        asks=[BookLevel(price=0.50, size=10)],
    )
    result = MicrostructureEstimator().estimate(crossed, market_id="m1", category="other")
    assert result.reject_reason == "micro_no_quote"
    assert result.estimate is None
    with pytest.raises(MicrostructureReject) as exc:
        fair_value(0.50, 0.40, 10, 10)
    assert exc.value.reason == "micro_no_quote"


def test_fee_aware_edge_uses_existing_fee_and_min_edge():
    fair, bid, ask = 0.45, 0.40, 0.50
    fee_buy = fee_per_share(ask, "crypto")
    fee_sell = fee_per_share(bid, "crypto")
    assert fee_buy == pytest.approx(0.07 * ask * (1 - ask))
    assert buy_yes_edge(fair, ask, fee_buy) == pytest.approx(fair - ask - fee_buy)
    assert sell_no_edge(bid, fair, fee_sell) == pytest.approx(bid - fair - fee_sell)
    # Inside the spread the fee-aware edge is negative, so MIN_EDGE 0.05 misses.
    assert buy_yes_edge(fair, ask, fee_buy) < 0.05
    assert sell_no_edge(bid, fair, fee_sell) < 0.05
    assert select_side(buy_yes_edge(fair, ask, fee_buy), sell_no_edge(bid, fair, fee_sell), 0.05) is None

    assert select_side(0.06, -1.0, 0.05) == "BUY_YES"
    assert select_side(-1.0, 0.06, 0.05) == "SELL_NO"
    assert select_side(0.04, 0.04, 0.05) is None
    assert select_side(0.05, 0.05, 0.05) == "BUY_YES"
    assert select_side(0.05, 0.08, 0.05) == "SELL_NO"
    # Boundary is inclusive.
    assert select_side(0.05, -1.0, 0.05) == "BUY_YES"

    # Fees can hide a raw edge that would otherwise clear MIN_EDGE.
    assert fee_hides_raw_edge(
        buy_edge=0.04, sell_edge=-1.0, raw_yes=0.06, raw_no=-1.0, min_edge=0.05
    )
    assert not fee_hides_raw_edge(
        buy_edge=0.05, sell_edge=-1.0, raw_yes=0.07, raw_no=-1.0, min_edge=0.05
    )


def test_confidence_proxy_and_components():
    assert MICRO_MIN_CONFIDENCE == 0.50
    assert MICRO_MIN_CONFIDENCE == GEMINI_MIN_CONFIDENCE
    assert GEMINI_KELLY_MULTIPLIER == 0.125  # unchanged; micro path does not apply it
    assert confidence_proxy(1, 1, 1) == 1.0
    assert confidence_proxy(1, 0, 0) == pytest.approx(1 / 3)
    assert confidence_proxy(1.0, 2 / 3, 0.0) == pytest.approx((1 + 2 / 3) / 3)

    assert depth_score(0.40, 0.42, 10_000, 10_000, 500) == 1.0
    assert depth_score(0.40, 0.42, 1, 1, 500) == pytest.approx(0.82 / 500)
    assert depth_score(0.40, 0.42, 1, 1, 0) == 0.0

    assert spread_score(0.0, 0.06) == 1.0
    assert spread_score(0.03, 0.06) == pytest.approx(0.5)
    assert spread_score(0.06, 0.06) == 0.0
    assert spread_score(0.12, 0.06) == 0.0
    assert spread_score(0.02, 0) == 0.0

    # |I| stability needs 3 snapshots. Constant |I| scores 1. A full flip scores 0.
    assert imbalance_stability([0.2]) == 0.0
    assert imbalance_stability([0.2, 0.2]) == 0.0
    assert imbalance_stability([0.2, 0.2, 0.2]) == 1.0
    assert imbalance_stability([0.0, 1.0, 0.0]) == 0.0
    # Only the last three samples count, so a leading flip is ignored.
    assert imbalance_stability([1.0, 0.2, 0.2, 0.2]) == 1.0


def test_estimator_confidence_edge_and_snapshot_window():
    deep = book(bid=0.40, ask=0.42, bid_size=10_000, ask_size=10_000)
    est = MicrostructureEstimator(min_edge=0.05, max_spread=0.06, min_liquidity=500)
    first = est.estimate(deep, market_id="m1", category="politics")
    assert first.reject_reason is None
    assert first.estimate is not None
    assert first.confidence_score is not None
    assert first.confidence_score >= MICRO_MIN_CONFIDENCE
    assert first.stability == 0.0  # only one snapshot
    assert first.side is None
    assert first.buy_yes_edge is not None and first.buy_yes_edge < 0.05
    assert first.sell_no_edge is not None and first.sell_no_edge < 0.05
    assert first.estimate.should_abstain is False
    assert first.estimate.estimated_probability == pytest.approx(first.fair)
    assert "not an event forecast" in first.estimate.reasoning_summary

    est.estimate(deep, market_id="m1", category="politics")
    third = est.estimate(deep, market_id="m1", category="politics")
    assert third.stability == 1.0
    assert len(est.snapshots("m1")) == 3
    assert third.confidence_score is not None
    assert third.confidence_score > first.confidence_score

    # A fourth snapshot drops the oldest. Another market does not share history.
    wider = book(bid=0.40, ask=0.42, bid_size=1, ask_size=9_000)
    est.estimate(wider, market_id="m1", category="politics")
    assert len(est.snapshots("m1")) == 3
    assert est.snapshots("m1")[-1].bid_size == 1
    other = est.estimate(deep, market_id="m2", category="politics")
    assert len(est.snapshots("m2")) == 1
    assert other.stability == 0.0

    # Floor cannot be cut below 0.50. A thin top still abstains if the caller asks.
    thin = OrderBook(
        token_id="yes1",
        market_id="m-thin",
        bids=[BookLevel(price=0.40, size=1), BookLevel(price=0.38, size=5_000)],
        asks=[BookLevel(price=0.42, size=1), BookLevel(price=0.44, size=5_000)],
    )
    low = est.estimate(thin, market_id="m-thin", category="politics", min_confidence=0.0)
    assert low.estimate is not None
    assert low.confidence_score is not None
    assert low.confidence_score < MICRO_MIN_CONFIDENCE
    assert low.estimate.should_abstain is True
    assert low.estimate.abstention_reason == "micro_low_confidence"


def test_estimator_tie_prefers_buy_yes_when_min_edge_is_negative():
    """Both fee-aware edges clear only if MIN_EDGE is below the spread."""
    deep = book(bid=0.40, ask=0.42, bid_size=10_000, ask_size=10_000)
    est = MicrostructureEstimator(min_edge=-1.0, max_spread=0.06, min_liquidity=500)
    result = est.estimate(deep, market_id="m1", category="geopolitics", min_edge=-1.0)
    assert fee_per_share(0.42, "geopolitics") == 0.0
    assert result.side == "BUY_YES"
    assert result.estimate is not None
    assert result.estimate.should_abstain is False
    assert result.fair is not None
    assert result.fair == pytest.approx((0.40 + 0.42) / 2)


def test_estimator_abstains_when_fees_hide_a_touching_fair(tmp_path):
    """I = 1 puts fair on the ask. Raw edge is 0; the fee makes it a miss."""
    touched = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.40, size=10_000)],
        asks=[BookLevel(price=0.42, size=0.0), BookLevel(price=0.44, size=5_000)],
    )
    est = MicrostructureEstimator(min_edge=0.0, max_spread=0.06, min_liquidity=500)
    result = est.estimate(touched, market_id="m1", category="crypto", min_edge=0.0)
    assert result.imbalance == pytest.approx(1.0)
    assert result.fair == pytest.approx(0.42)
    assert result.buy_yes_edge is not None and result.buy_yes_edge < 0
    assert result.side is None
    assert result.estimate is not None
    assert result.estimate.should_abstain is True
    assert result.estimate.abstention_reason == "micro_edge"
    # Strategy must not be handed a tradable probability that ignores the fee.
    decision = StrategyEvaluator(settings(tmp_path, min_edge=0.0)).evaluate(
        market=market(category="crypto"),
        book=touched,
        estimate=result.estimate,
        packet=object(),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
        min_edge=0.0,
        min_confidence_score=MICRO_MIN_CONFIDENCE,
    )
    assert decision.approved is False
    assert decision.reject_reason == "grok_abstain"


def test_strategy_pipeline_still_rejects_micro_quotes(tmp_path):
    """Same Survival gates. A confident inside-spread fair does not trade."""
    s = settings(tmp_path)
    assert s.kelly_multiplier == 0.25
    assert s.max_spread == 0.06
    assert s.min_edge == 0.05
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90
    deep = book(bid=0.40, ask=0.42, bid_size=10_000, ask_size=10_000)
    result = MicrostructureEstimator().estimate(deep, market_id="m1", category="politics")
    assert result.estimate is not None
    decision = StrategyEvaluator(s).evaluate(
        market=market(),
        book=deep,
        estimate=result.estimate,
        packet=object(),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
        min_confidence_score=MICRO_MIN_CONFIDENCE,
    )
    assert decision.approved is False
    assert decision.reject_reason == "edge_too_small"

    thin = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.40, size=1), BookLevel(price=0.38, size=5_000)],
        asks=[BookLevel(price=0.42, size=1), BookLevel(price=0.44, size=5_000)],
    )
    low = MicrostructureEstimator().estimate(thin, market_id="m1", category="politics")
    assert low.estimate is not None
    abstained = StrategyEvaluator(s).evaluate(
        market=market(),
        book=thin,
        estimate=low.estimate,
        packet=object(),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
        min_confidence_score=MICRO_MIN_CONFIDENCE,
    )
    assert abstained.approved is False
    assert abstained.reject_reason == "grok_abstain"


def test_spend_block_is_budget_only():
    budget = RegimeState(
        "DIE",
        "ai_session_budget 10.0000>=10.0000",
        10,
        10.0,
        50,
        50,
        screening_allowed=False,
    )
    stop = RegimeState(
        "ATTACK",
        "screening_stop unrealized 1.0000<burn 4.0000",
        4,
        4.0,
        50,
        50,
        screening_allowed=False,
        has_taken_fills=True,
    )
    defend_stop = RegimeState(
        "DEFEND",
        "screening_stop unrealized 0.0000<burn 4.0000; burn_half_cushion 4.0000",
        4,
        4.0,
        50,
        50,
        screening_allowed=False,
        has_taken_fills=True,
    )
    kill = RegimeState(
        "DIE",
        "kill_floor equity=30.00<=40.00",
        10,
        10.0,
        30,
        50,
        screening_allowed=False,
    )
    weekly = RegimeState(
        "DIE",
        "weekly_equity_stop equity=45.00<=47.50 baseline=50.00",
        0,
        0.0,
        45,
        50,
        screening_allowed=False,
    )
    healthy = RegimeState("ATTACK", "healthy", 0, 0.0, 50, 50, screening_allowed=True)
    assert ai_spend_blocked_screening(budget)
    assert ai_spend_blocked_screening(stop)
    assert ai_spend_blocked_screening(defend_stop)
    assert not ai_spend_blocked_screening(kill)
    assert not ai_spend_blocked_screening(weekly)
    assert not ai_spend_blocked_screening(healthy)
