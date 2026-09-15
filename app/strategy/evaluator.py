"""Edge, EV, Kelly, decision gates. Grok never sizes."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.schemas import MarketEstimate
from app.config import Settings
from app.market_data.models import Market, OrderBook
from app.market_data.orderbook import FillEstimate, walk_book
from app.research.researcher import EvidencePacket


TAKER_FEE_RATE = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "geopolitics": 0.0,
    "mentions": 0.04,
    "tech": 0.04,
    "other": 0.05,
}


def fee_per_share(price: float, category: str) -> float:
    rate = TAKER_FEE_RATE.get(category.lower(), 0.05)
    p = min(max(price, 1e-6), 1 - 1e-6)
    return rate * p * (1 - p)


def full_kelly(p: float, c: float) -> float:
    if c <= 0 or c >= 1 or p <= 0 or p >= 1:
        return 0.0
    b = (1.0 - c) / c
    q = 1.0 - p
    f = ((b * p) - q) / b
    return max(0.0, f)


def quarter_kelly(p: float, c: float, multiplier: float = 0.25) -> float:
    return full_kelly(p, c) * multiplier


def expected_profit_per_share(p: float, c: float) -> float:
    return p - c


def expected_return_on_cost(p: float, c: float) -> float:
    if c <= 0:
        return 0.0
    return (p - c) / c


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Decision:
    approved: bool
    reject_reason: str | None
    gates: list[Gate]
    market_id: str
    token_id: str | None = None
    side: str | None = None
    grok_p: float | None = None
    market_price: float | None = None
    raw_edge: float | None = None
    execution_adjusted_edge: float | None = None
    expected_profit_per_share: float | None = None
    expected_return_on_cost: float | None = None
    kelly: float | None = None
    size_usd: float = 0.0
    size_shares: float = 0.0
    limit_price: float | None = None
    fill: FillEstimate | None = None
    correlation_group: str = ""
    category: str = ""

    def gates_dict(self) -> dict[str, dict]:
        return {g.name: {"passed": g.passed, "detail": g.detail} for g in self.gates}


def _fail(gates: list[Gate], reason: str, **kwargs) -> Decision:
    return Decision(approved=False, reject_reason=reason, gates=gates, **kwargs)


class StrategyEvaluator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        *,
        market: Market,
        book: OrderBook,
        estimate: MarketEstimate | None,
        packet: EvidencePacket | None,
        bankroll: float,
        cash: float,
        existing_market_exposure: float,
        existing_category_exposure: float,
        existing_total_exposure: float,
        existing_group_exposure: float,
        duplicate: bool,
        halted: bool,
        broker_ok: bool,
        data_fresh: bool,
        canary: bool = False,
        open_positions: int = 0,
        canary_day_notional: float = 0.0,
        min_edge: float | None = None,
    ) -> Decision:
        s = self.settings
        edge_floor = s.min_edge if min_edge is None else min_edge
        gates: list[Gate] = []
        base = dict(market_id=market.market_id, category=market.category, correlation_group=market.correlation_group)

        def g(name: str, ok: bool, detail: str = "") -> bool:
            gates.append(Gate(name, ok, detail))
            return ok

        if not g("market_valid", market.active and not market.closed and not market.paused, market.status):
            return _fail(gates, "market_invalid", **base)
        if not g("data_fresh", data_fresh, f"age={book.age_seconds():.2f}s"):
            return _fail(gates, "stale_data", **base)
        if not g("resolution_understandable", bool(market.resolution_criteria or market.description)):
            return _fail(gates, "unclear_resolution", **base)
        liq = book.ask_depth_usd()
        if not g("liquidity_sufficient", liq >= s.min_liquidity, f"ask_depth={liq:.2f}"):
            return _fail(gates, "insufficient_liquidity", **base)
        spread_ok = book.spread is not None and book.spread <= s.max_spread
        if not g("spread_acceptable", bool(spread_ok), str(book.spread)):
            return _fail(gates, "spread_too_wide", **base)
        if not g("research_available", packet is not None):
            return _fail(gates, "no_research", **base)
        if not g("grok_response_valid", estimate is not None):
            return _fail(gates, "invalid_grok", **base)
        assert estimate is not None
        if not g("grok_not_abstain", not estimate.should_abstain, estimate.abstention_reason):
            return _fail(gates, "grok_abstain", **base, grok_p=estimate.estimated_probability)
        if not g("confidence_sufficient", estimate.confidence_score >= s.min_confidence_score, str(estimate.confidence_score)):
            return _fail(gates, "low_confidence", **base, grok_p=estimate.estimated_probability)

        p_yes = estimate.estimated_probability
        yes_ask = book.best_ask
        # NO book is not this book; we only have the YES token book here.
        # Caller should pass the book for the token being considered.
        if yes_ask is None:
            g("edge_sufficient", False, "no_ask")
            return _fail(gates, "no_executable_price", **base)

        # Decide YES vs NO from which token this book belongs to
        side = "BUY"
        if book.token_id == market.no_token_id:
            p = 1.0 - p_yes
            token_id = market.no_token_id
        else:
            p = p_yes
            token_id = market.yes_token_id
        px = book.best_ask
        if px is None:
            return _fail(gates, "no_executable_price", **base)
        raw_edge = p - px
        if not g("edge_sufficient", raw_edge >= edge_floor, f"raw_edge={raw_edge:.4f} floor={edge_floor:.4f}"):
            return _fail(gates, "edge_too_small", **base, grok_p=p, market_price=px, raw_edge=raw_edge)

        ev = expected_profit_per_share(p, px)
        if not g("expected_value_positive", ev > 0, f"ev={ev:.4f}"):
            return _fail(gates, "nonpositive_ev", **base, grok_p=p, market_price=px, raw_edge=raw_edge)

        kelly_f = quarter_kelly(p, px, s.kelly_multiplier)
        if not g("kelly_positive", kelly_f > 0, f"kelly={kelly_f:.4f}"):
            return _fail(gates, "kelly_zero", **base, grok_p=p, market_price=px, raw_edge=raw_edge, kelly=kelly_f)

        if not g("kill_switch_inactive", not halted):
            return _fail(gates, "halted", **base)
        if not g("broker_operational", broker_ok):
            return _fail(gates, "broker_down", **base)
        if not g("duplicate_absent", not duplicate):
            return _fail(gates, "duplicate_order", **base)

        size_usd = kelly_f * bankroll
        size_usd = min(size_usd, s.max_position_pct_bankroll * bankroll)
        size_usd = min(size_usd, max(0.0, s.max_market_exposure_pct * bankroll - existing_market_exposure))
        size_usd = min(size_usd, max(0.0, s.max_category_exposure_pct * bankroll - existing_category_exposure))
        size_usd = min(size_usd, max(0.0, s.max_correlation_group_exposure_pct * bankroll - existing_group_exposure))
        size_usd = min(size_usd, max(0.0, s.max_total_exposure_pct * bankroll - existing_total_exposure))
        size_usd = min(size_usd, cash)

        if canary:
            size_usd = min(size_usd, s.canary_max_order_usd)
            size_usd = min(size_usd, max(0.0, s.canary_max_daily_notional_usd - canary_day_notional))
            if open_positions >= s.canary_max_open_positions:
                g("position_limit_available", False, "canary_open_positions")
                return _fail(gates, "canary_open_positions", **base)

        # liquidity-adjusted: notional <= depth / min_liquidity_multiple
        max_by_liq = liq / s.min_liquidity_multiple if s.min_liquidity_multiple else size_usd
        size_usd = min(size_usd, max_by_liq)

        g("position_limit_available", size_usd > 0, f"size_usd={size_usd:.4f}")
        g("portfolio_exposure_available", size_usd > 0)
        g("correlation_exposure_acceptable", size_usd > 0)

        if size_usd <= 0:
            return _fail(gates, "size_zero_after_limits", **base, grok_p=p, market_price=px, raw_edge=raw_edge, kelly=kelly_f)

        shares = size_usd / px
        if shares < market.min_order_size:
            g("balance_sufficient", False, "below_min_order")
            return _fail(gates, "below_min_order_size", **base, grok_p=p, market_price=px)

        fill = walk_book(book, "BUY", shares)
        fee = fee_per_share(fill.vwap if fill.filled_shares else px, market.category)
        exec_px = fill.vwap if fill.filled_shares else px
        exec_edge = p - exec_px - fee
        slip_ok = fill.slippage_pct <= s.max_slippage_pct
        g("slippage_acceptable", slip_ok, f"slip={fill.slippage_pct:.4f}")
        g("execution_adjusted_edge_positive", exec_edge > 0, f"exec_edge={exec_edge:.4f}")
        g("balance_sufficient", cash >= fill.notional if fill.filled_shares else False, f"cash={cash:.2f}")

        if not slip_ok:
            return _fail(gates, "slippage_too_high", **base, grok_p=p, market_price=px, raw_edge=raw_edge, execution_adjusted_edge=exec_edge, kelly=kelly_f, fill=fill, token_id=token_id, side=side)
        if exec_edge <= 0:
            return _fail(gates, "execution_edge_nonpositive", **base, grok_p=p, market_price=px, raw_edge=raw_edge, execution_adjusted_edge=exec_edge, kelly=kelly_f, fill=fill, token_id=token_id, side=side)
        if not fill.filled_shares:
            return _fail(gates, "no_fill_estimate", **base, grok_p=p, market_price=px, fill=fill, token_id=token_id, side=side)

        size_shares = fill.filled_shares
        size_usd = fill.notional
        return Decision(
            approved=True,
            reject_reason=None,
            gates=gates,
            market_id=market.market_id,
            token_id=token_id,
            side=side,
            grok_p=p,
            market_price=px,
            raw_edge=raw_edge,
            execution_adjusted_edge=exec_edge,
            expected_profit_per_share=ev,
            expected_return_on_cost=expected_return_on_cost(p, px),
            kelly=kelly_f,
            size_usd=size_usd,
            size_shares=size_shares,
            limit_price=fill.vwap,
            fill=fill,
            correlation_group=market.correlation_group,
            category=market.category,
        )
