"""Rule-based book estimator. It does not forecast the event.

Used when ``ESTIMATOR=microstructure`` and the AI session budget has stopped
new LLM screening (or when a test forces it on). With
``ESTIMATOR_AUTO_SWITCH`` (default ON), the loop can flip back to an LLM when
daily realized PnL covers burn and burn is under the session budget, then
return here when realized slips under burn or burn hits the budget. No LLM
call on this path.

Phase 1 fair value (replaces the Phase 0 half-spread cap):

    microprice m* = (bid_size * ask + ask_size * bid) / (bid_size + ask_size)
    imbalance  I  = (bid_size - ask_size) / (bid_size + ask_size)  in [-1, 1]
    Δ̂            = λ × I          λ = MICRO_LAMBDA (default 0.08)
    fair          = clip(mid + Δ̂, 0.01, 0.99)

``m*`` and ``I`` are telemetry and inputs. Fair value is the clipped
displacement, not the microprice, and ``|fair − mid|`` is not capped at the
half-spread. ``bid_size + ask_size <= 0`` fails closed (no division).

Fee-aware entry, using ``fee_per_share``. Same comparison as Phase 0; the
fair value above is what changed:

    BUY YES  iff  fair - ask - fee(ask) >= MIN_EDGE
    sell/NO  iff  bid - fair - fee(bid) >= MIN_EDGE

Extra hard filters on this path only. Survival gates are not loosened
(``MIN_EDGE``, ``MAX_SPREAD``, mid band, Kelly 0.25, kill, weekly, caps).

* ``|I| >= MICRO_MIN_ABS_I`` (default 0.40). Weaker books reject
  ``micro_weak_imbalance``.
* Book mid in ``[MICRO_COIN_FLIP_MIN, MICRO_COIN_FLIP_MAX]`` (defaults
  0.45–0.55 inclusive) rejects ``micro_coin_flip_mid``. New micro entries
  only; shared ``MIN_TRADEABLE_MID`` / Gemini path unchanged.
* ``ask - bid <= MAX_SPREAD`` stays on the existing book filter and
  ``StrategyEvaluator``. This module does not open a wider spread.
* Touch size (shares at the top of the side we would take) must be at least
  3× the intended order. Thinner books reject ``micro_thin_touch``.
  Intended size is the caller's share count when one is passed. Otherwise,
  when bankroll is known, it is the quarter-Kelly notional capped by
  ``position_notional_cap`` and cash, divided by the trade price. BUY YES
  uses ``(fair, ask)``. Sell/NO uses the complementary buy ``(1 − fair,
  1 − bid)`` and compares that share count to the YES bid, which is the
  touch on this book. When Kelly is not available, the conservative proxy
  is ``MAX_POSITION_USD / mid`` if that cap is set, else the position-cap
  notional divided by mid. With neither bankroll nor ``MAX_POSITION_USD``,
  intended size is 0 and the depth check does not invent a size.

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
from app.strategy.evaluator import fee_per_share, quarter_kelly

# Same number as the Survival confidence floor. Never lowered.
MICRO_MIN_CONFIDENCE = 0.50
MICRO_PROVIDER = "micro"
MICRO_LAMBDA_DEFAULT = 0.08
MICRO_MIN_ABS_I = 0.40
# Inclusive coin-flip mid band. Micro new entries only.
MICRO_COIN_FLIP_MIN = 0.45
MICRO_COIN_FLIP_MAX = 0.55
MICRO_COIN_FLIP_REJECT = "micro_coin_flip_mid"
# Touch shares must be at least this many times the intended order.
MICRO_TOUCH_MULTIPLE = 3.0
FAIR_CLIP_LO = 0.01
FAIR_CLIP_HI = 0.99
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


def clip_fair(value: float, lo: float = FAIR_CLIP_LO, hi: float = FAIR_CLIP_HI) -> float:
    """Clip fair into ``[0.01, 0.99]``. Does not pull it back inside the spread."""
    if value > hi:
        return hi
    if value < lo:
        return lo
    return value


def predicted_displacement(imb: float, lam: float) -> float:
    """Δ̂ = λ × I. Not capped by the spread."""
    return lam * imb


def fair_value(
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    lam: float = MICRO_LAMBDA_DEFAULT,
) -> float:
    """fair = clip(mid + λ × I, 0.01, 0.99).

    Requires a positive spread. Zero size fails closed via ``imbalance``.
    ``|fair − mid|`` is the clipped displacement, not the half-spread.
    """
    if not _finite(bid) or not _finite(ask) or not (ask > bid):
        raise MicrostructureReject("micro_no_quote")
    if not _finite(lam):
        raise MicrostructureReject("micro_no_quote")
    mid = (bid + ask) / 2.0
    delta = predicted_displacement(imbalance(bid_size, ask_size), lam)
    return clip_fair(mid + delta)


def position_cap_notional(
    bankroll: float, max_position_pct_bankroll: float, max_position_usd: float | None
) -> float:
    """Same stricter-of rule as ``app.risk.caps.position_notional_cap``."""
    cap = max(0.0, max_position_pct_bankroll) * bankroll
    if max_position_usd is not None:
        cap = min(cap, max_position_usd)
    return max(0.0, cap)


def resolve_intended_shares(
    *,
    mid: float,
    trade_price: float,
    probability: float,
    bankroll: float | None,
    cash: float | None,
    kelly_multiplier: float,
    max_position_usd: float | None,
    max_position_pct_bankroll: float,
    explicit_shares: float | None,
) -> tuple[float, str]:
    """Shares the 3× touch check compares against, and where they came from.

    ``explicit`` — caller already knows the order size.
    ``kelly`` — quarter-Kelly notional, then the position cap and cash.
    ``position_cap`` — Kelly was not positive; use the full position cap / mid
    so a missing size does not skip the depth check.
    ``max_position_usd`` — no bankroll; ``MAX_POSITION_USD / mid``.
    ``unknown`` — neither bankroll nor ``MAX_POSITION_USD``. Size is 0.
    """
    if explicit_shares is not None and _finite(explicit_shares) and explicit_shares >= 0:
        return float(explicit_shares), "explicit"

    proxy_px = mid if _finite(mid) and mid > 0 else trade_price

    def _proxy() -> tuple[float, str] | None:
        if bankroll is not None and _finite(bankroll) and bankroll > 0 and proxy_px > 0:
            cap = position_cap_notional(
                bankroll, max_position_pct_bankroll, max_position_usd
            )
            if cap > 0:
                return cap / proxy_px, "position_cap"
        if (
            max_position_usd is not None
            and _finite(max_position_usd)
            and max_position_usd > 0
            and proxy_px > 0
        ):
            return max_position_usd / proxy_px, "max_position_usd"
        return None

    kelly_known = (
        bankroll is not None
        and _finite(bankroll)
        and bankroll > 0
        and _finite(trade_price)
        and 0.0 < trade_price < 1.0
        and _finite(probability)
        and 0.0 < probability < 1.0
        and _finite(kelly_multiplier)
    )
    if kelly_known:
        assert bankroll is not None
        kelly_f = quarter_kelly(probability, trade_price, kelly_multiplier)
        notional = kelly_f * bankroll
        notional = min(
            notional,
            position_cap_notional(bankroll, max_position_pct_bankroll, max_position_usd),
        )
        if cash is not None and _finite(cash):
            notional = min(notional, max(0.0, cash))
        if notional > 0:
            return notional / trade_price, "kelly"
        if cash is not None and _finite(cash) and cash <= 0:
            return 0.0, "kelly"

    prox = _proxy()
    if prox is not None:
        return prox
    return 0.0, "unknown"


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
    lam: float | None = None
    displacement: float | None = None
    intended_shares: float | None = None
    touch_size: float | None = None
    size_source: str | None = None

    def as_fields(self) -> dict:
        return {
            "microprice": self.microprice,
            "imbalance": self.imbalance,
            "lambda": self.lam,
            "displacement": self.displacement,
            "fair": self.fair,
            "buy_yes_edge": self.buy_yes_edge,
            "sell_no_edge": self.sell_no_edge,
            "confidence_score": self.confidence_score,
            "depth_score": self.depth_score,
            "spread_score": self.spread_score,
            "stability": self.stability,
            "side": self.side,
            "intended_shares": self.intended_shares,
            "touch_size": self.touch_size,
            "size_source": self.size_source,
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
        lam: float = MICRO_LAMBDA_DEFAULT,
        min_abs_imbalance: float = MICRO_MIN_ABS_I,
        coin_flip_min: float = MICRO_COIN_FLIP_MIN,
        coin_flip_max: float = MICRO_COIN_FLIP_MAX,
        bankroll: float | None = None,
        cash: float | None = None,
        kelly_multiplier: float = 0.25,
        max_position_usd: float | None = None,
        max_position_pct_bankroll: float = 0.03,
        touch_multiple: float = MICRO_TOUCH_MULTIPLE,
    ) -> None:
        self.min_edge = min_edge
        self.max_spread = max_spread
        self.min_confidence = min_confidence
        self.min_liquidity = min_liquidity
        self.lam = lam
        self.min_abs_imbalance = min_abs_imbalance
        self.coin_flip_min = coin_flip_min
        self.coin_flip_max = coin_flip_max
        self.bankroll = bankroll
        self.cash = cash
        self.kelly_multiplier = kelly_multiplier
        self.max_position_usd = max_position_usd
        self.max_position_pct_bankroll = max_position_pct_bankroll
        # The 3× touch rule is a floor. A higher multiple stays; a lower one does not.
        self.touch_multiple = max(MICRO_TOUCH_MULTIPLE, touch_multiple)
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
        intended_shares: float | None = None,
        bankroll: float | None = None,
        cash: float | None = None,
        kelly_multiplier: float | None = None,
    ) -> MicrostructureResult:
        edge_floor = self.min_edge if min_edge is None else min_edge
        conf_floor = self.min_confidence if min_confidence is None else min_confidence
        # A configured floor above 0.50 stays. The Survival floor is never cut.
        conf_floor = max(MICRO_MIN_CONFIDENCE, conf_floor)
        roll = self.bankroll if bankroll is None else bankroll
        cash_now = self.cash if cash is None else cash
        kelly_mult = self.kelly_multiplier if kelly_multiplier is None else kelly_multiplier
        try:
            bid, ask, bid_size, ask_size = _top(book)
            m_star = microprice(bid, ask, bid_size, ask_size)
            imb = imbalance(bid_size, ask_size)
            delta = predicted_displacement(imb, self.lam)
            fair = fair_value(bid, ask, bid_size, ask_size, self.lam)
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
        mid = (bid + ask) / 2.0

        common = dict(
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
            lam=self.lam,
            displacement=delta,
        )

        # Inclusive coin-flip band. Micro new entries only (this estimator).
        if self.coin_flip_min <= mid <= self.coin_flip_max:
            return MicrostructureResult(
                estimate=None,
                reject_reason=MICRO_COIN_FLIP_REJECT,
                **common,
            )

        if abs(imb) < self.min_abs_imbalance:
            return MicrostructureResult(
                estimate=None,
                reject_reason="micro_weak_imbalance",
                **common,
            )

        shares: float | None = None
        touch: float | None = None
        source: str | None = None
        if side is not None:
            if side == "BUY_YES":
                prob, px, touch = fair, ask, ask_size
            else:
                # Complementary NO buy. The touch on this YES book is the bid.
                prob, px, touch = (1.0 - fair), (1.0 - bid), bid_size
            shares, source = resolve_intended_shares(
                mid=mid,
                trade_price=px,
                probability=prob,
                bankroll=roll,
                cash=cash_now,
                kelly_multiplier=kelly_mult,
                max_position_usd=self.max_position_usd,
                max_position_pct_bankroll=self.max_position_pct_bankroll,
                explicit_shares=intended_shares,
            )
            common["intended_shares"] = shares
            common["touch_size"] = touch
            common["size_source"] = source
            if touch < self.touch_multiple * shares:
                return MicrostructureResult(
                    estimate=None,
                    reject_reason="micro_thin_touch",
                    **common,
                )

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

        if not (0.0 <= fair <= 1.0) or not _finite(fair):
            return MicrostructureResult(
                estimate=None,
                reject_reason="micro_no_quote",
                **common,
            )

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
                f"lambda={self.lam:.6f}",
                f"displacement={delta:.6f}",
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
                f"lambda={self.lam:.4f} displacement={delta:.4f} "
                f"buy_edge={yes_edge:.4f} sell_edge={no_edge:.4f} "
                f"side={side or 'none'}; not an event forecast"
            ),
        )
        return MicrostructureResult(
            estimate=estimate,
            reject_reason=None,
            **common,
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
