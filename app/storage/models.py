from __future__ import annotations

from pydantic import BaseModel, Field


class SystemState(BaseModel):
    paper_trading_started_at: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    trading_mode: str = "paper"
    live_activated_at: str | None = None
    consecutive_losses: int = 0
