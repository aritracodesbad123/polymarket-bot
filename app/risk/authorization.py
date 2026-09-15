"""Hard live lock. Every condition is required. Fail closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE POLYMARKET TRADING"
PAPER_MIN_SECONDS = 7 * 24 * 60 * 60


@dataclass
class LiveAuthInput:
    now: datetime
    paper_trading_started_at: datetime | None
    halted: bool
    live_trading_enabled: bool
    has_activation_record: bool
    activation_phrase_ok: bool
    has_valid_live_credentials: bool
    trading_mode_env: str


@dataclass
class LiveAuthResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    eligible: bool = False  # 7 days elapsed, still not live

    @property
    def fail_closed(self) -> bool:
        return not self.allowed


class LiveAuthorization(Protocol):
    def evaluate(self, inp: LiveAuthInput) -> LiveAuthResult: ...


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate_live_authorization(inp: LiveAuthInput) -> LiveAuthResult:
    reasons: list[str] = []
    now = inp.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if inp.halted:
        reasons.append("system_halted")

    started = inp.paper_trading_started_at
    if started is None:
        reasons.append("paper_clock_not_started")
        elapsed = 0.0
    else:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (now - started).total_seconds()
        if elapsed < PAPER_MIN_SECONDS:
            reasons.append("paper_duration_under_7_days")

    eligible = started is not None and elapsed >= PAPER_MIN_SECONDS

    if inp.trading_mode_env != "live":
        reasons.append("trading_mode_not_live")
    if not inp.live_trading_enabled:
        reasons.append("LIVE_TRADING_ENABLED_false")
    if not inp.has_activation_record:
        reasons.append("no_human_activation_record")
    if not inp.activation_phrase_ok:
        reasons.append("activation_phrase_invalid")
    if not inp.has_valid_live_credentials:
        reasons.append("live_credentials_missing")

    return LiveAuthResult(allowed=len(reasons) == 0, reasons=reasons, eligible=eligible)


def auth_from_state(
    *,
    now: datetime,
    paper_trading_started_at: str | None,
    halted: bool,
    live_trading_enabled: bool,
    has_activation_record: bool,
    activation_phrase: str | None,
    has_valid_live_credentials: bool,
    trading_mode_env: str,
) -> LiveAuthResult:
    return evaluate_live_authorization(
        LiveAuthInput(
            now=now,
            paper_trading_started_at=parse_ts(paper_trading_started_at),
            halted=halted,
            live_trading_enabled=live_trading_enabled,
            has_activation_record=has_activation_record,
            activation_phrase_ok=activation_phrase == LIVE_CONFIRMATION_PHRASE,
            has_valid_live_credentials=has_valid_live_credentials,
            trading_mode_env=trading_mode_env,
        )
    )
