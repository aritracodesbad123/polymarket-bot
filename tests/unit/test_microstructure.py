"""Phase 1 microstructure fair value, fee-aware edge, and hard filters."""

from __future__ import annotations

import pytest

from app.ai.microstructure import (
    MICRO_COIN_FLIP_MAX,
    MICRO_COIN_FLIP_MIN,
    MICRO_COIN_FLIP_REJECT,
    MICRO_LAMBDA_DEFAULT,
    MICRO_MIN_ABS_I,
    MICRO_MIN_CONFIDENCE,
    MICRO_TOUCH_MULTIPLE,
    MicrostructureEstimator,
    MicrostructureReject,
    buy_yes_edge,
    confidence_proxy,
    depth_score,
    fair_value,
    fee_hides_raw_edge,
    imbalance,
    imbalance_stability,
    microprice,
    position_cap_notional,
    resolve_intended_shares,
    select_side,
    sell_no_edge,
    spread_score,
)
from app.main import GEMINI_KELLY_MULTIPLIER, GEMINI_MIN_CONFIDENCE
from app.market_data.models import BookLevel, OrderBook
from app.market_data.scanner import filter_book
from app.risk.caps import position_notional_cap
from app.risk.regime import RegimeState, ai_spend_blocked_screening
from app.strategy.evaluator import StrategyEvaluator, fee_per_share, quarter_kelly
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


def test_lambda_moves_mid_eight_cents_and_fair_can_leave_the_spread():
    """λ=0.08 at |I|=1 is an 8¢ shift. That shift is not pulled back to the spread."""
    assert MICRO_LAMBDA_DEFAULT == 0.08
    bid, ask = 0.40, 0.42
    mid = 0.41
    hi = fair_value(bid, ask, 5, 0, lam=0.08)
    lo = fair_value(bid, ask, 0, 5, lam=0.08)
    assert hi == pytest.approx(mid + 0.08)
    assert lo == pytest.approx(mid - 0.08)
    # Half-spread is 1¢, so the uncapped 8¢ shift sits outside the quotes.
    assert hi > ask
    assert lo < bid
    # Telemetry only: microprice is still the size-weighted quote, not fair.
    assert microprice(bid, ask, 5, 0) == pytest.approx(ask)
    assert hi != pytest.approx(microprice(bid, ask, 5, 0))

    # A wider spread can still contain the 8¢ shift. That is the displacement,
    # not a half-spread cap. Half-spread here is 10¢.
    wide = fair_value(0.40, 0.60, 5, 0, lam=0.08)
    assert wide == pytest.approx(0.50 + 0.08)
    assert 0.40 < wide < 0.60


def test_fair_clips_outside_one_cent_to_ninety_nine_cents():
    hi = fair_value(0.96, 0.98, 10, 0, lam=0.08)
    assert (0.96 + 0.98) / 2 + 0.08 > 0.99
    assert hi == pytest.approx(0.99)
    lo = fair_value(0.02, 0.04, 0, 10, lam=0.08)
    assert (0.02 + 0.04) / 2 - 0.08 < 0.01
    assert lo == pytest.approx(0.01)


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


def test_weak_imbalance_rejects_and_boundary_passes():
    assert MICRO_MIN_ABS_I == 0.40
    mild = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.40, size=6_000)],
        asks=[BookLevel(price=0.42, size=4_000)],
    )
    assert imbalance(6_000, 4_000) == pytest.approx(0.2)
    est = MicrostructureEstimator()
    rejected = est.estimate(mild, market_id="m1", category="politics")
    assert rejected.reject_reason == "micro_weak_imbalance"
    assert rejected.estimate is None
    assert rejected.imbalance == pytest.approx(0.2)
    assert rejected.microprice is not None
    assert rejected.fair == pytest.approx(0.41 + 0.08 * 0.2)
    assert rejected.displacement == pytest.approx(0.08 * 0.2)
    # The quote was real, so it still enters the stability window.
    assert len(est.snapshots("m1")) == 1

    # |I| == 0.40 is enough. A hair under is not.
    at_floor = OrderBook(
        token_id="yes1",
        market_id="m-floor",
        bids=[BookLevel(price=0.40, size=7_000)],
        asks=[BookLevel(price=0.42, size=3_000)],
    )
    assert imbalance(7_000, 3_000) == pytest.approx(0.40)
    held = est.estimate(at_floor, market_id="m-floor", category="politics")
    assert held.reject_reason is None
    assert held.estimate is not None

    under = OrderBook(
        token_id="yes1",
        market_id="m-under",
        bids=[BookLevel(price=0.40, size=6_990)],
        asks=[BookLevel(price=0.42, size=3_010)],
    )
    assert abs(imbalance(6_990, 3_010)) < 0.40
    weak = est.estimate(under, market_id="m-under", category="politics")
    assert weak.reject_reason == "micro_weak_imbalance"


def test_coin_flip_mid_rejects_and_outside_can_proceed():
    """Inclusive [0.45, 0.55] blocks micro new entries. Outside can pass this gate."""
    assert MICRO_COIN_FLIP_MIN == 0.45
    assert MICRO_COIN_FLIP_MAX == 0.55
    assert MICRO_COIN_FLIP_REJECT == "micro_coin_flip_mid"
    est = MicrostructureEstimator()

    # Mid 0.50 with strong |I| still dies on the coin-flip band first.
    coin = OrderBook(
        token_id="yes1",
        market_id="m-coin",
        bids=[BookLevel(price=0.49, size=9_000)],
        asks=[BookLevel(price=0.51, size=1_000)],
    )
    assert (0.49 + 0.51) / 2.0 == pytest.approx(0.50)
    assert imbalance(9_000, 1_000) == pytest.approx(0.8)
    rejected = est.estimate(coin, market_id="m-coin", category="politics")
    assert rejected.reject_reason == MICRO_COIN_FLIP_REJECT
    assert rejected.estimate is None
    assert rejected.imbalance == pytest.approx(0.8)
    assert rejected.fair is not None

    # Inclusive edges of the band.
    lo = book(bid=0.44, ask=0.46, bid_size=9_000, ask_size=1_000)
    hi = book(bid=0.54, ask=0.56, bid_size=9_000, ask_size=1_000)
    assert lo.midpoint == pytest.approx(0.45)
    assert hi.midpoint == pytest.approx(0.55)
    assert (
        est.estimate(lo, market_id="m-lo", category="politics").reject_reason
        == MICRO_COIN_FLIP_REJECT
    )
    assert (
        est.estimate(hi, market_id="m-hi", category="politics").reject_reason
        == MICRO_COIN_FLIP_REJECT
    )

    # Mid 0.40 / 0.60 clear this gate (other gates may still reject later).
    below = book(bid=0.39, ask=0.41, bid_size=9_000, ask_size=1_000)
    above = book(bid=0.59, ask=0.61, bid_size=9_000, ask_size=1_000)
    assert below.midpoint == pytest.approx(0.40)
    assert above.midpoint == pytest.approx(0.60)
    for mid_book, mid_id in ((below, "m-below"), (above, "m-above")):
        held = est.estimate(mid_book, market_id=mid_id, category="politics")
        assert held.reject_reason != MICRO_COIN_FLIP_REJECT
        assert held.reject_reason is None
        assert held.estimate is not None


def test_fee_aware_edge_clears_min_edge_when_imbalance_is_strong():
    """MIN_EDGE stays 0.05. Default λ=0.08 at |I|=1 is an 8¢ shift and clears it here."""
    min_edge = 0.05
    bid, ask = 0.48, 0.50
    mid = (bid + ask) / 2.0
    fee = fee_per_share(ask, "geopolitics")
    assert fee == 0.0
    # Default λ at |I|=1 is +8¢. Half-spread is 1¢, so the fee-aware edge is 7¢.
    fair_default = fair_value(bid, ask, 10, 0, lam=MICRO_LAMBDA_DEFAULT)
    assert fair_default == pytest.approx(mid + 0.08)
    assert fair_default > ask
    edge_default = buy_yes_edge(fair_default, ask, fee)
    assert edge_default == pytest.approx(fair_default - ask)
    assert edge_default == pytest.approx(0.07)
    assert edge_default >= min_edge
    assert select_side(edge_default, -1.0, min_edge) == "BUY_YES"

    # Same formula, a larger λ. |I|=0.40 misses MIN_EDGE; |I|=1 clears it.
    mild = fair_value(bid, ask, 7, 3, lam=0.12)
    strong = fair_value(bid, ask, 10, 0, lam=0.12)
    assert imbalance(7, 3) == pytest.approx(0.40)
    assert buy_yes_edge(mild, ask, fee) < min_edge
    assert buy_yes_edge(strong, ask, fee) >= min_edge
    assert select_side(buy_yes_edge(strong, ask, fee), -1.0, min_edge) == "BUY_YES"

    # Inside-spread fair still cannot clear a non-negative MIN_EDGE after fees.
    inside = 0.45
    fee_buy = fee_per_share(0.50, "crypto")
    assert buy_yes_edge(inside, 0.50, fee_buy) < 0
    assert select_side(0.06, -1.0, min_edge) == "BUY_YES"
    assert select_side(-1.0, 0.06, min_edge) == "SELL_NO"
    assert select_side(0.04, 0.04, min_edge) is None
    assert select_side(min_edge, min_edge, min_edge) == "BUY_YES"
    assert select_side(min_edge, 0.08, min_edge) == "SELL_NO"
    assert fee_hides_raw_edge(
        buy_edge=0.04, sell_edge=-1.0, raw_yes=0.06, raw_no=-1.0, min_edge=min_edge
    )
    assert not fee_hides_raw_edge(
        buy_edge=0.05, sell_edge=-1.0, raw_yes=0.07, raw_no=-1.0, min_edge=min_edge
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
    # |I| = 0.8 so the quote is not a weak-imbalance reject. Default λ=0.08
    # puts the raw edge over MIN_EDGE; the politics fee pulls it back under,
    # so the estimate abstains.
    deep = book(bid=0.40, ask=0.42, bid_size=9_000, ask_size=1_000)
    est = MicrostructureEstimator(min_edge=0.05, max_spread=0.06, min_liquidity=500)
    first = est.estimate(deep, market_id="m1", category="politics")
    assert first.reject_reason is None
    assert first.estimate is not None
    assert first.imbalance == pytest.approx(0.8)
    assert first.fair == pytest.approx(0.41 + 0.08 * 0.8)
    assert first.confidence_score is not None
    assert first.confidence_score >= MICRO_MIN_CONFIDENCE
    assert first.stability == 0.0  # only one snapshot
    assert first.side is None
    assert first.buy_yes_edge is not None and first.buy_yes_edge < 0.05
    assert first.sell_no_edge is not None and first.sell_no_edge < 0.05
    assert first.estimate.should_abstain is True
    assert first.estimate.abstention_reason == "micro_edge"
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

    # Floor cannot be cut below 0.50. A thin top with |I| >= 0.40 still abstains.
    thin = OrderBook(
        token_id="yes1",
        market_id="m-thin",
        bids=[BookLevel(price=0.40, size=1), BookLevel(price=0.38, size=5_000)],
        asks=[BookLevel(price=0.42, size=5), BookLevel(price=0.44, size=5_000)],
    )
    low = est.estimate(thin, market_id="m-thin", category="politics", min_confidence=0.0)
    assert low.reject_reason is None
    assert low.estimate is not None
    assert low.confidence_score is not None
    assert low.confidence_score < MICRO_MIN_CONFIDENCE
    assert low.estimate.should_abstain is True
    assert low.estimate.abstention_reason == "micro_low_confidence"


def test_positive_imbalance_prefers_buy_yes_when_min_edge_is_negative():
    deep = book(bid=0.40, ask=0.42, bid_size=9_000, ask_size=1_000)
    est = MicrostructureEstimator(min_edge=-1.0, max_spread=0.06, min_liquidity=500)
    result = est.estimate(deep, market_id="m1", category="geopolitics", min_edge=-1.0)
    assert fee_per_share(0.42, "geopolitics") == 0.0
    assert result.side == "BUY_YES"
    assert result.estimate is not None
    assert result.estimate.should_abstain is False
    assert result.fair == pytest.approx(0.41 + 0.08 * 0.8)
    assert result.fair > 0.42


def test_estimator_abstains_when_fees_hide_a_clearing_raw_edge(tmp_path):
    """Raw edge clears MIN_EDGE; the taker fee pulls the fee-aware edge under it."""
    # Mid 0.59 is outside the coin-flip band. I = 0.8, λ = 0.08, half-spread = 1¢
    # → raw edge ≈ 5.4¢. Crypto fee hides it.
    strong = OrderBook(
        token_id="yes1",
        market_id="m-strong",
        bids=[BookLevel(price=0.58, size=9_000)],
        asks=[BookLevel(price=0.60, size=1_000)],
    )
    est = MicrostructureEstimator(
        min_edge=0.05,
        max_spread=0.06,
        min_liquidity=500,
        lam=0.08,
    )
    hidden = est.estimate(strong, market_id="m-strong", category="crypto", min_edge=0.05)
    assert hidden.reject_reason is None
    assert hidden.side is None
    assert hidden.estimate is not None
    assert hidden.estimate.should_abstain is True
    assert hidden.estimate.abstention_reason == "micro_edge"
    assert hidden.buy_yes_edge is not None and hidden.buy_yes_edge < 0.05
    assert hidden.fair is not None and (hidden.fair - 0.60) >= 0.05
    decision = StrategyEvaluator(settings(tmp_path, min_edge=0.05)).evaluate(
        market=market(category="crypto"),
        book=strong,
        estimate=hidden.estimate,
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
        min_edge=0.05,
        min_confidence_score=MICRO_MIN_CONFIDENCE,
    )
    assert decision.approved is False
    assert decision.reject_reason == "grok_abstain"


def test_strong_imbalance_clears_unchanged_min_edge(tmp_path):
    """A larger λ can trade. λ=0.04 on this book still dies on unchanged MIN_EDGE."""
    s = settings(tmp_path)
    assert s.min_edge == 0.05
    assert s.kelly_multiplier == 0.25
    assert s.max_spread == 0.06
    bid, ask = 0.38, 0.40
    strong_book = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=bid, size=9_000)],
        asks=[BookLevel(price=ask, size=2_000)],
    )
    default = MicrostructureEstimator(
        min_edge=0.05,
        lam=0.04,
        bankroll=1000,
        max_position_pct_bankroll=0.03,
        kelly_multiplier=0.25,
    ).estimate(strong_book, market_id="m1", category="geopolitics")
    assert default.reject_reason is None
    assert default.side is None
    assert default.estimate is not None
    assert default.estimate.should_abstain is False
    missed = StrategyEvaluator(s).evaluate(
        market=market(category="geopolitics"),
        book=strong_book,
        estimate=default.estimate,
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
    assert missed.approved is False
    assert missed.reject_reason == "edge_too_small"

    opened = MicrostructureEstimator(
        min_edge=0.05,
        lam=0.12,
        bankroll=1000,
        max_position_pct_bankroll=0.03,
        kelly_multiplier=0.25,
        min_liquidity=500,
    ).estimate(strong_book, market_id="m-open", category="geopolitics")
    assert opened.reject_reason is None
    assert opened.side == "BUY_YES"
    assert opened.buy_yes_edge is not None and opened.buy_yes_edge >= 0.05
    assert opened.estimate is not None
    assert opened.estimate.should_abstain is False
    decision = StrategyEvaluator(s).evaluate(
        market=market(category="geopolitics"),
        book=strong_book,
        estimate=opened.estimate,
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
    assert decision.approved is True
    assert decision.reject_reason is None
    assert decision.raw_edge is not None and decision.raw_edge >= 0.05


def test_thin_touch_uses_position_proxy_and_kelly_size():
    assert MICRO_TOUCH_MULTIPLE == 3.0
    bid, ask = 0.38, 0.40
    mid = 0.39
    # No bankroll: intended shares = MAX_POSITION_USD / mid.
    proxy_shares = 25.0 / mid
    thin = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=bid, size=10_000)],
        asks=[BookLevel(price=ask, size=proxy_shares * 3 - 1)],
    )
    est = MicrostructureEstimator(
        min_edge=0.05,
        lam=0.20,
        max_position_usd=25.0,
        min_liquidity=1.0,
    )
    rejected = est.estimate(thin, market_id="m1", category="geopolitics")
    assert rejected.side == "BUY_YES"
    assert rejected.size_source == "max_position_usd"
    assert rejected.intended_shares == pytest.approx(proxy_shares)
    assert rejected.reject_reason == "micro_thin_touch"
    assert rejected.estimate is None

    exact = OrderBook(
        token_id="yes1",
        market_id="m-exact",
        bids=[BookLevel(price=bid, size=10_000)],
        asks=[BookLevel(price=ask, size=proxy_shares * 3)],
    )
    held = est.estimate(exact, market_id="m-exact", category="geopolitics")
    assert held.reject_reason is None
    assert held.side == "BUY_YES"
    assert held.touch_size == pytest.approx(proxy_shares * 3)

    # Explicit size wins over the proxy.
    forced = est.estimate(
        exact,
        market_id="m-force",
        category="geopolitics",
        intended_shares=10_000,
    )
    assert forced.reject_reason == "micro_thin_touch"
    assert forced.size_source == "explicit"
    assert forced.intended_shares == pytest.approx(10_000)

    # Kelly size when bankroll is known and the fraction is under the cap.
    # Complementary check on the bid uses the NO buy when I is negative.
    # Mid 0.59 stays outside the coin-flip band.
    sell_book = OrderBook(
        token_id="yes1",
        market_id="m-sell",
        bids=[BookLevel(price=0.58, size=10)],
        asks=[BookLevel(price=0.60, size=10_000)],
    )
    kelly_est = MicrostructureEstimator(
        min_edge=0.05,
        lam=0.066,
        bankroll=1000,
        kelly_multiplier=0.25,
        max_position_pct_bankroll=0.03,
        max_position_usd=None,
        min_liquidity=1.0,
    )
    sold = kelly_est.estimate(sell_book, market_id="m-sell", category="geopolitics")
    assert sold.imbalance is not None and sold.imbalance < -0.40
    assert sold.side == "SELL_NO"
    assert sold.fair is not None
    no_p = 1.0 - sold.fair
    no_px = 1.0 - 0.58
    cap = position_cap_notional(1000, 0.03, None)
    kelly_notional = quarter_kelly(no_p, no_px, 0.25) * 1000
    assert 0 < kelly_notional < cap
    assert sold.size_source == "kelly"
    assert sold.intended_shares == pytest.approx(kelly_notional / no_px)
    assert sold.intended_shares is not None
    assert sold.touch_size == pytest.approx(10)
    assert sold.intended_shares > 0
    assert sold.reject_reason == "micro_thin_touch"

    # A weak imbalance rejects before the touch check, even if the touch is thin.
    # Mid outside the coin-flip band so the reason stays micro_weak_imbalance.
    both = OrderBook(
        token_id="yes1",
        market_id="m-both",
        bids=[BookLevel(price=0.38, size=1)],
        asks=[BookLevel(price=0.40, size=1)],
    )
    first = est.estimate(both, market_id="m-both", category="geopolitics")
    assert first.reject_reason == "micro_weak_imbalance"


def test_position_cap_proxy_matches_sizing_helper(tmp_path):
    s = settings(tmp_path, max_position_usd=25.0, paper_starting_bankroll=1000)
    assert position_cap_notional(1000, s.max_position_pct_bankroll, 25.0) == (
        position_notional_cap(s, 1000)
    )
    shares, source = resolve_intended_shares(
        mid=0.50,
        trade_price=0.50,
        probability=0.50,
        bankroll=None,
        cash=None,
        kelly_multiplier=0.25,
        max_position_usd=25.0,
        max_position_pct_bankroll=0.03,
        explicit_shares=None,
    )
    assert source == "max_position_usd"
    assert shares == pytest.approx(25.0 / 0.50)


def test_strategy_pipeline_still_rejects_micro_quotes(tmp_path):
    """Same Survival gates. Default λ=0.08 on this politics book abstains and is not approved."""
    s = settings(tmp_path)
    assert s.kelly_multiplier == 0.25
    assert s.max_spread == 0.06
    assert s.min_edge == 0.05
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90
    deep = book(bid=0.40, ask=0.42, bid_size=9_000, ask_size=1_000)
    result = MicrostructureEstimator().estimate(deep, market_id="m1", category="politics")
    assert result.estimate is not None
    assert result.estimate.should_abstain is True
    assert result.fair is not None and result.fair > 0.42
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
    assert decision.reject_reason == "grok_abstain"

    wide = book(bid=0.40, ask=0.50, bid_size=9_000, ask_size=1_000)
    assert filter_book(wide, s) == "spread_too_wide"

    thin = OrderBook(
        token_id="yes1",
        market_id="m1",
        bids=[BookLevel(price=0.40, size=1), BookLevel(price=0.38, size=5_000)],
        asks=[BookLevel(price=0.42, size=5), BookLevel(price=0.44, size=5_000)],
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


def test_touch_multiple_cannot_drop_below_three():
    est = MicrostructureEstimator(touch_multiple=1.0)
    assert est.touch_multiple == 3.0


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
