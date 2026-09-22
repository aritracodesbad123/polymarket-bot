"""Stricter-of percentage and optional absolute USD caps.

Absolute caps are optional. When unset, only the existing percentage limits apply.

``MAX_POSITION_USD`` (and the percentage position cap, whichever is stricter)
is a cost-basis lock: ``shares × avg_price`` plus any resting BUY reserve on
that token. A new entry is sized down to the cap. An add-on buy is not clipped
to the leftover room — if cost plus the buy would finish above the cap, the
whole add is rejected. An already-oversize ticket is left in place; only
further buys that would increase the breach are blocked.
"""

from __future__ import annotations

from app.config import Settings

# Float noise only. Not a budget to print through the cap.
CAP_EPS = 1e-6


def position_notional_cap(settings: Settings, bankroll: float) -> float:
    cap = max(0.0, settings.max_position_pct_bankroll) * bankroll
    if settings.max_position_usd is not None:
        cap = min(cap, settings.max_position_usd)
    return max(0.0, cap)


def total_exposure_cap(settings: Settings, bankroll: float) -> float:
    cap = max(0.0, settings.max_total_exposure_pct) * bankroll
    if settings.max_total_exposure_usd is not None:
        cap = min(cap, settings.max_total_exposure_usd)
    return max(0.0, cap)


def position_cost_usd(shares: float, avg_price: float) -> float:
    """Cost basis of one open ticket. Flat inventory is zero."""
    if shares <= 1e-12:
        return 0.0
    return max(0.0, float(shares)) * max(0.0, float(avg_price))


def position_cost_from_rows(positions, token_id: str) -> float:
    """Sum ``shares × avg_price`` for ``token_id`` across open rows."""
    total = 0.0
    for p in positions or []:
        if isinstance(p, dict):
            tid = p.get("token_id")
            shares = float(p.get("shares") or 0.0)
            avg = float(p.get("avg_price") or 0.0)
        else:
            tid = getattr(p, "token_id", None)
            shares = float(getattr(p, "shares", 0.0) or 0.0)
            avg = float(getattr(p, "avg_price", 0.0) or 0.0)
        if tid != token_id:
            continue
        total += position_cost_usd(shares, avg)
    return total


def add_breaches_cap(existing: float, add_usd: float, cap: float) -> bool:
    """True when a BUY that increases notional would finish above ``cap``.

    A non-positive add does not increase the position. Equality with the cap
    is allowed; anything above ``cap + CAP_EPS`` is a breach.
    """
    if add_usd <= CAP_EPS:
        return False
    return float(existing) + float(add_usd) > float(cap) + CAP_EPS


def daily_loss_cap_usd(settings: Settings, bankroll: float) -> float | None:
    """Effective daily realized-loss cap in dollars.

    None when MAX_DAILY_LOSS_USD is unset — the percentage kill is unchanged.
    When set, the cap is the stricter of the percentage limit and the absolute limit.
    """
    if settings.max_daily_loss_usd is None:
        return None
    pct = max(0.0, settings.max_daily_loss_pct) * bankroll
    return min(pct, max(0.0, settings.max_daily_loss_usd))
