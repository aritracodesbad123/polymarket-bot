"""ATTACK / DEFEND / DIE — pay for yourself or stop burning capital and API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings


@dataclass
class RegimeState:
    mode: str  # ATTACK | DEFEND | DIE
    reason: str
    session_ai_calls: int
    session_ai_cost_usd: float
    equity: float
    start_bankroll: float


class RegimeEngine:
    """In-memory day counters (ponytail: restart resets burn; upgrade = persist to system_state)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session_ai_calls = 0
        self._day = datetime.now(timezone.utc).date()
        self.last: RegimeState | None = None

    def _roll_day(self) -> None:
        d = datetime.now(timezone.utc).date()
        if d != self._day:
            self._day = d
            self.session_ai_calls = 0

    def note_ai_call(self, n: int = 1) -> None:
        self._roll_day()
        self.session_ai_calls += max(0, n)

    @property
    def session_ai_cost_usd(self) -> float:
        return self.session_ai_calls * self.settings.estimated_usd_per_ai_call

    def update(self, *, equity: float, unrealized_pnl: float) -> RegimeState:
        self._roll_day()
        s = self.settings
        start = s.paper_starting_bankroll
        burn = self.session_ai_cost_usd
        kill_floor = start * (1.0 - s.kill_floor_pct)
        profit = max(0.0, equity - start)
        die_burn_line = profit + s.api_die_cushion_usd

        if not s.regime_enabled:
            st = RegimeState("ATTACK", "regime_disabled", self.session_ai_calls, burn, equity, start)
            self.last = st
            return st

        if equity <= kill_floor:
            st = RegimeState("DIE", f"kill_floor equity={equity:.2f}<={kill_floor:.2f}", self.session_ai_calls, burn, equity, start)
            self.last = st
            return st
        if burn >= die_burn_line:
            st = RegimeState(
                "DIE",
                f"api_burn {burn:.4f}>={die_burn_line:.4f}",
                self.session_ai_calls,
                burn,
                equity,
                start,
            )
            self.last = st
            return st

        defend = False
        reason = "healthy"
        if equity < start - s.api_die_cushion_usd:
            defend = True
            reason = f"equity_below_start {equity:.2f}<{start:.2f}"
        elif unrealized_pnl < -s.api_die_cushion_usd:
            defend = True
            reason = f"unrealized {unrealized_pnl:.4f}"
        elif burn >= die_burn_line * 0.5 and die_burn_line > 0:
            defend = True
            reason = f"burn_half_cushion {burn:.4f}"

        mode = "DEFEND" if defend else "ATTACK"
        st = RegimeState(mode, reason, self.session_ai_calls, burn, equity, start)
        self.last = st
        return st

    def evaluate_kwargs(self, mode: str | None = None) -> dict:
        m = mode or (self.last.mode if self.last else "ATTACK")
        if m != "DEFEND":
            return {}
        s = self.settings
        return {
            "min_edge": s.min_edge + s.defend_edge_tighten,
            "kelly_multiplier": s.kelly_multiplier * s.defend_kelly_mult,
        }

    def max_ai_calls(self, mode: str | None = None) -> int:
        m = mode or (self.last.mode if self.last else "ATTACK")
        if m == "DIE":
            return 0
        if m == "DEFEND":
            return min(self.settings.max_grok_calls_per_cycle, self.settings.defend_max_grok_calls)
        return self.settings.max_grok_calls_per_cycle
