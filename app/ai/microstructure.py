"""Rule-based book estimator. It does not forecast the event.

Used only when ``ESTIMATOR=microstructure`` and the AI session budget has
stopped new screening (or when a test forces it on). No LLM call.

Locked quotes (top of book):

    microprice m* = (bid_size * ask + ask_size * bid) / (bid_size + ask_size)
    imbalance  I  = (bid_size - ask_size) / (bid_size + ask_size)  in [-1, 1]
    fair          = mid + 0.5 * I * (ask - bid)
                    with |fair - mid| capped at the half-spread

``bid_size + ask_size <= 0`` fails closed (no division).

Fee-aware entry, using ``fee_per_share``:

    BUY YES  iff  fair - ask - fee(ask) >= MIN_EDGE
    sell/NO  iff  bid - fair - fee(bid) >= MIN_EDGE

Fair value sits inside the spread, so both edges are <= -fee. With
``MIN_EDGE >= 0`` the entry does not clear. The quote still goes through
``StrategyEvaluator``; the existing edge, EV, Kelly, spread, and exposure
gates reject it. Those gates are not loosened here.

Confidence proxy (replaces the LLM score). Trade requires >= 0.50, or a
higher configured ``MIN_CONFIDENCE_SCORE``:

    depth_score  = clamp(top_notional / MIN_LIQUIDITY, 0, 1)
                   top_notional = bid * bid_size + ask * ask_size
                   MIN_LIQUIDITY <= 0 scores 0 (no division)
    spread_score = clamp(1 - spread / MAX_SPREAD, 0, 1)
                   MAX_SPREAD <= 0 scores 0
    stability    = |I| stability over the last 3 top-of-book snapshots
                   for this market, including the one just taken.
                   Fewer than 3 snapshots scores 0 (undefined).
                   Otherwise clamp(1 - mean(| |I|_t - |I|_{t-1} |), 0, 1).
                   Constant |I| scores 1. A 0↔1 flip each step scores 0.
    confidence   = (depth_score + spread_score + stability) / 3
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Literal

from app.ai.schemas import MarketEstimate
from app.market_data.models import OrderBook
from app.strategy.evaluator import fee_per_share

# Same number as the Survival confidence floor. Never lowered.
MICRO_MIN_CONFIDENCE = 0.50
MICRO_PROVIDER = "micro"
_HISTORY = 3


class MicrostructureReject(Exception):
    """Fail closed. ``reason`` is a trade-decision reject code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _total_size(bid_size: float, ask_size: float) -> float:
    if not _finite(bid_size) or not _finite(ask_size) or bid_size < 0 or ask_size < 0:
        raise MicrostructureReject("micro_zero_size")
    total = bid_size + ask_size
    if not (total > 0):
        raise MicrostructureReject("micro_zero_size")
    return total


def microprice(bid: float, ask: float, bid_size: float, ask_size: float) -> float:
    """m* = (bid_size * ask + ask_size * bid) / (bid_size + ask_size)."""
    total = _total_size(bid_size, ask_size)
    return (bid_size * ask + ask_size * bid) / total


def imbalance(bid_size: float, ask_size: float) -> float:
    """I = (bid_size - ask_size) / (bid_size + ask_size), clamped to [-1, 1]."""
    total = _total_size(bid_size, ask_size)
    raw = (bid_size - ask_size) / total
    if raw > 1.0:
        return 1.0
    if raw < -1.0:
        return -1.0
    return raw


def cap_fair_to_half_spread(fair: float, mid: float, half_spread: float) -> float:
    """Cap |fair - mid| at the half-spread."""
    half = abs(half_spread)
    if fair > mid + half:
        return mid + half
    if fair < mid - half:
        return mid - half
    return fair


def fair_value(bid: float, ask: float, bid_size: float, ask_size: float) -> float:
    """fair = mid + 0.5 * I * (ask - bid), capped at the half-spread.

    Requires a positive spread. Zero size fails closed via ``imbalance``.
    """
    if not _finite(bid) or not _finite(ask) or not (ask > bid):
        raise MicrostructureReject("micro_no_quote")
    mid = (bid + ask) / 2.0
    spread = ask - bid
    raw = mid + 0.5 * imbalance(bid_size, ask_size) * spread
    return cap_fair_to_half_spread(raw, mid, spread / 2.0)


def buy_yes_edge(fair: float, ask: float, fee: float) -> float:
    """fair - ask - fees."""
    return fair - ask - fee


def sell_no_edge(bid: float, fair: float, fee: float) -> float:
    """bid - fair - fees. This is the sell-YES / buy-NO edge on the YES book."""
    return bid - fair - fee


def select_side(buy_edge: float, sell_edge: float, min_edge: float) -> str | None:
    """``BUY_YES`` or ``SELL_NO`` when that fee-aware edge clears ``min_edge``.

    A tie (both clear, equal edge) takes BUY YES. Neither clearing returns None.
    """
    buy_ok = buy_edge >= min_edge
    sell_ok = sell_edge >= min_edge
    if buy_ok and sell_ok:
        return "BUY_YES" if buy_edge >= sell_edge else "SELL_NO"
    if buy_ok:
        return "BUY_YES"
    if sell_ok:
        return "SELL_NO"
    return None


def fee_hides_raw_edge(
    *,
    buy_edge: float,
    sell_edge: float,
    raw_yes: float,
    raw_no: float,
    min_edge: float,
) -> bool:
    """True when fees knock the edge under MIN_EDGE but raw p-price would pass.

    The strategy gate is raw (fair - ask). This is the extra fail-closed so a
    fee-aware miss cannot become a fill.
    """
    if buy_edge >= min_edge or sell_edge >= min_edge:
        return False
    return raw_yes >= min_edge or raw_no >= min_edge


def depth_score(
    bid: float, ask: float, bid_size: float, ask_size: float, min_liquidity: float
) -> float:
    """clamp((bid * bid_size + ask * ask_size) / MIN_LIQUIDITY, 0, 1)."""
    if not _finite(min_liquidity) or min_liquidity <= 0:
        return 0.0
    if not _finite(bid) or not _finite(ask) or bid_size < 0 or ask_size < 0:
        return 0.0
    notional = bid * bid_size + ask * ask_size
    if not _finite(notional) or notional <= 0:
        return 0.0
    return min(1.0, notional / min_liquidity)


def spread_score(spread: float, max_spread: float) -> float:
    """clamp(1 - spread / MAX_SPREAD, 0, 1)."""
    if not _finite(max_spread) or max_spread <= 0 or not _finite(spread):
        return 0.0
    return max(0.0, min(1.0, 1.0 - spread / max_spread))


def imbalance_stability(abs_imbalances: list[float]) -> float:
    """|I| stability over the last 3 snapshots. Fewer than 3 scores 0.

    stability = clamp(1 - mean(|a_t - a_{t-1}|), 0, 1) on the last three
    absolute imbalances. Constant |I| scores 1.
    """
    window = [abs(x) for x in abs_imbalances][-_HISTORY:]
    if len(window) < _HISTORY:
        return 0.0
    deltas = [abs(window[i] - window[i - 1]) for i in range(1, len(window))]
    mean_delta = sum(deltas) / len(deltas)
    return max(0.0, min(1.0, 1.0 - mean_delta))


def confidence_proxy(depth: float, spread: float, stability: float) -> float:
    """Average of depth score, spread score, and |I| stability. In [0, 1]."""
    score = (depth + spread + stability) / 3.0
    if not _finite(score):
        return 0.0
    return max(0.0, min(1.0, score))


def _confidence_label(score: float) -> Literal["low", "medium", "high"]:
    if score >= 0.75:
        return "high"
    if score >= MICRO_MIN_CONFIDENCE:
        return "medium"
    return "low"


@dataclass(frozen=True)
class TopSnapshot:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    imbalance: float


@dataclass
class MicrostructureResult:
    estimate: MarketEstimate | None
    reject_reason: str | None
    side: str | None = None
    microprice: float | None = None
    imbalance: float | None = None
    fair: float | None = None
    buy_yes_edge: float | None = None
    sell_no_edge: float | None = None
    confidence_score: float | None = None
    depth_score: float | None = None
    spread_score: float | None = None
    stability: float | None = None

    def as_fields(self) -> dict:
        return {
            "microprice": self.microprice,
            "imbalance": self.imbalance,
            "fair": self.fair,
            "buy_yes_edge": self.buy_yes_edge,
            "sell_no_edge": self.sell_no_edge,
            "confidence_score": self.confidence_score,
            "depth_score": self.depth_score,
            "spread_score": self.spread_score,
            "stability": self.stability,
            "side": self.side,
        }


class MicrostructureEstimator:
    """Per-market top-of-book history (last 3) and a ``MarketEstimate``."""

    def __init__(
        self,
        *,
        min_edge: float = 0.05,
        max_spread: float = 0.06,
        min_confidence: float = MICRO_MIN_CONFIDENCE,
        min_liquidity: float = 500.0,
    ) -> None:
        self.min_edge = min_edge
        self.max_spread = max_spread
        self.min_confidence = min_confidence
        self.min_liquidity = min_liquidity
        self._history: dict[str, deque[TopSnapshot]] = {}

    def snapshots(self, market_id: str) -> list[TopSnapshot]:
        return list(self._history.get(market_id, ()))

    def estimate(
        self,
        book: OrderBook,
        *,
        market_id: str,
        category: str,
        min_edge: float | None = None,
        min_confidence: float | None = None,
    ) -> MicrostructureResult:
        edge_floor = self.min_edge if min_edge is None else min_edge
        conf_floor = self.min_confidence if min_confidence is None else min_confidence
        # A configured floor above 0.50 stays. The Survival floor is never cut.
        conf_floor = max(MICRO_MIN_CONFIDENCE, conf_floor)
        try:
            bid, ask, bid_size, ask_size = _top(book)
            m_star = microprice(bid, ask, bid_size, ask_size)
            imb = imbalance(bid_size, ask_size)
            fair = fair_value(bid, ask, bid_size, ask_size)
        except MicrostructureReject as exc:
            return MicrostructureResult(estimate=None, reject_reason=exc.reason)

        hist = self._history.setdefault(market_id, deque(maxlen=_HISTORY))
        hist.append(
            TopSnapshot(
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                imbalance=imb,
            )
        )
        dscore = depth_score(bid, ask, bid_size, ask_size, self.min_liquidity)
        sscore = spread_score(ask - bid, self.max_spread)
        stability = imbalance_stability([abs(s.imbalance) for s in hist])
        conf = confidence_proxy(dscore, sscore, stability)
        fee_buy = fee_per_share(ask, category or "other")
        fee_sell = fee_per_share(bid, category or "other")
        yes_edge = buy_yes_edge(fair, ask, fee_buy)
        no_edge = sell_no_edge(bid, fair, fee_sell)
        side = select_side(yes_edge, no_edge, edge_floor)
        raw_yes = fair - ask
        raw_no = bid - fair
        abstain = False
        abstain_reason = ""
        if conf < conf_floor:
            abstain = True
            abstain_reason = "micro_low_confidence"
        elif side is None and fee_hides_raw_edge(
            buy_edge=yes_edge,
            sell_edge=no_edge,
            raw_yes=raw_yes,
            raw_no=raw_no,
            min_edge=edge_floor,
        ):
            abstain = True
            abstain_reason = "micro_edge"

        if not (0.0 <= fair <= 1.0):
            return MicrostructureResult(
                estimate=None,
                reject_reason="micro_no_quote",
                side=None,
                microprice=m_star,
                imbalance=imb,
                fair=fair,
                buy_yes_edge=yes_edge,
                sell_no_edge=no_edge,
                confidence_score=conf,
                depth_score=dscore,
                spread_score=sscore,
                stability=stability,
            )

        mid = (bid + ask) / 2.0
        estimate = MarketEstimate(
            market_id=market_id,
            estimated_probability=fair,
            confidence=_confidence_label(conf),
            confidence_score=conf,
            base_rate_probability=min(1.0, max(0.0, mid)),
            evidence_adjustment=fair - mid,
            key_evidence=[
                f"microprice={m_star:.6f}",
                f"imbalance={imb:.6f}",
                f"fair={fair:.6f}",
                "not_an_event_forecast",
            ],
            counterarguments=[
                "short-horizon book imbalance only; does not forecast the event"
            ],
            uncertainty_factors=[
                f"depth_score={dscore:.4f}",
                f"spread_score={sscore:.4f}",
                f"abs_imbalance_stability={stability:.4f}",
                f"snapshots={len(hist)}",
            ],
            stale_information_risk="low",
            should_abstain=abstain,
            abstention_reason=abstain_reason,
            reasoning_summary=(
                f"microstructure fair={fair:.4f} I={imb:.4f} "
                f"buy_edge={yes_edge:.4f} sell_edge={no_edge:.4f} "
                f"side={side or 'none'}; not an event forecast"
            ),
        )
        return MicrostructureResult(
            estimate=estimate,
            reject_reason=None,
            side=side,
            microprice=m_star,
            imbalance=imb,
            fair=fair,
            buy_yes_edge=yes_edge,
            sell_no_edge=no_edge,
            confidence_score=conf,
            depth_score=dscore,
            spread_score=sscore,
            stability=stability,
        )


def _top(book: OrderBook) -> tuple[float, float, float, float]:
    if not book.bids or not book.asks:
        raise MicrostructureReject("micro_no_quote")
    bid = book.bids[0].price
    ask = book.asks[0].price
    bid_size = book.bids[0].size
    ask_size = book.asks[0].size
    if not _finite(bid) or not _finite(ask) or not (ask > bid):
        raise MicrostructureReject("micro_no_quote")
    return bid, ask, bid_size, ask_size
