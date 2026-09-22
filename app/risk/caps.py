"""Stricter-of percentage and optional absolute USD caps.

Absolute caps are optional. When unset, only the existing percentage limits apply.
"""

from __future__ import annotations

from app.config import Settings


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


def daily_loss_cap_usd(settings: Settings, bankroll: float) -> float | None:
    """Effective daily realized-loss cap in dollars.

    None when MAX_DAILY_LOSS_USD is unset — the percentage kill is unchanged.
    When set, the cap is the stricter of the percentage limit and the absolute limit.
    """
    if settings.max_daily_loss_usd is None:
        return None
    pct = max(0.0, settings.max_daily_loss_pct) * bankroll
    return min(pct, max(0.0, settings.max_daily_loss_usd))
