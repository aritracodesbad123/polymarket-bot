from __future__ import annotations

from pydantic import BaseModel, Field


class SystemState(BaseModel):
    paper_trading_started_at: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    trading_mode: str = "paper"
    live_activated_at: str | None = None
    consecutive_losses: int = 0
    ai_calls_utc_day: str | None = None
    ai_call_count: int = 0
    week_started_on: str | None = None
    week_baseline_equity: float | None = None
    daily_pnl_utc_day: str | None = None
    daily_realized_pnl: float = 0.0
