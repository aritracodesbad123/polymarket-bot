"""ATTACK / DEFEND / DIE — pay for yourself or stop burning capital and API.

Day-scoped AI burn and the weekly equity baseline live in system_state.
A restart must not zero the cost-versus-return DIE control. If that state
cannot be read or written, the engine fails closed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import Settings
from app.storage.db import DatabaseError
from app.storage.repositories import Repositories


def utc_day(now: datetime) -> date:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date()


def iso_week_monday(d: date) -> date:
    """Monday 00:00 UTC is the weekly boundary (ISO weekday)."""
    return d - timedelta(days=d.weekday())


@dataclass
class RegimeState:
    mode: str  # ATTACK | DEFEND | DIE
    reason: str
    session_ai_calls: int
    session_ai_cost_usd: float
    equity: float
    start_bankroll: float


class RegimeEngine:
    """Day-scoped AI burn and weekly equity stop.

    ``repo`` persists counters on system_state. Omit it only for pure in-memory
    math tests. Production always passes the repository.
    """

    def __init__(
        self,
        settings: Settings,
        repo: Repositories | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.session_ai_calls = 0
        self._day = utc_day(self._now())
        self._week_started_on: str | None = None
        self._week_baseline: float | None = None
        self.last: RegimeState | None = None
        if repo is not None:
            self._load()

    def _today(self) -> date:
        return utc_day(self._now())

    def _week_id(self, d: date | None = None) -> str:
        return iso_week_monday(d or self._today()).isoformat()

    def _load(self) -> None:
        assert self.repo is not None
        try:
            day, count = self.repo.ai_burn_state()
            week_on, week_base = self.repo.week_baseline_state()
        except DatabaseError:
            raise
        self._load_burn(day, count)
        self._load_week(week_on, week_base)

    def _load_burn(self, day: str | None, count: int) -> None:
        if count < 0:
            raise DatabaseError("ai_call_count_negative")
        today = self._today()
        if day is None:
            if count != 0:
                raise DatabaseError("ai_burn_count_without_day")
            self._day = today
            self.session_ai_calls = 0
            return
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise DatabaseError("ai_burn_day_invalid") from exc
        if parsed > today:
            raise DatabaseError("ai_burn_day_in_future")
        if parsed < today:
            self._day = today
            self.session_ai_calls = 0
            self._persist_burn()
            return
        self._day = today
        self.session_ai_calls = count

    def _load_week(self, week_on: str | None, week_base: float | None) -> None:
        if (week_on is None) != (week_base is None):
            raise DatabaseError("week_baseline_inconsistent")
        if week_on is None:
            self._week_started_on = None
            self._week_baseline = None
            return
        try:
            parsed = date.fromisoformat(week_on)
        except ValueError as exc:
            raise DatabaseError("week_start_invalid") from exc
        if week_base is None or week_base < 0:
            raise DatabaseError("week_baseline_invalid")
        current = date.fromisoformat(self._week_id())
        if parsed > current:
            raise DatabaseError("week_start_in_future")
        self._week_started_on = week_on
        self._week_baseline = float(week_base)

    def _persist_burn(self) -> None:
        if self.repo is None:
            return
        try:
            self.repo.set_ai_burn(self._day.isoformat(), self.session_ai_calls)
        except DatabaseError:
            self._halt("ai_burn_persist_failed")
            raise

    def _persist_week(self) -> None:
        if self.repo is None:
            return
        if self._week_started_on is None or self._week_baseline is None:
            raise DatabaseError("week_baseline_missing")
        try:
            self.repo.set_week_baseline(self._week_started_on, self._week_baseline)
        except DatabaseError:
            self._halt("week_baseline_persist_failed")
            raise

    def _halt(self, reason: str) -> None:
        if self.repo is None:
            return
        if not self.repo.state().halted:
            self.repo.halt(reason)

    def _roll_day(self) -> None:
        d = self._today()
        if d != self._day:
            self._day = d
            self.session_ai_calls = 0
            self._persist_burn()

    def _weekly_enabled(self) -> bool:
        pct = self.settings.weekly_loss_pct
        return pct is not None and pct > 0

    def _roll_week(self, equity: float) -> None:
        """Set the baseline on first use and on the Monday UTC boundary only."""
        if not self._weekly_enabled():
            return
        week_id = self._week_id()
        if self._week_started_on == week_id and self._week_baseline is not None:
            return
        if self._week_started_on is not None:
            stored = date.fromisoformat(self._week_started_on)
            if stored > date.fromisoformat(week_id):
                raise DatabaseError("week_start_in_future")
        self._week_started_on = week_id
        self._week_baseline = float(equity)
        self._persist_week()

    def reset_week_baseline(self, equity: float) -> None:
        """Intentional baseline reset. Does not clear a halt."""
        if equity < 0:
            raise DatabaseError("week_baseline_invalid")
        self._week_started_on = self._week_id()
        self._week_baseline = float(equity)
        self._persist_week()

    def note_ai_call(self, n: int = 1) -> None:
        self._roll_day()
        self.session_ai_calls += max(0, n)
        self._persist_burn()

    @property
    def session_ai_cost_usd(self) -> float:
        return self.session_ai_calls * self.settings.estimated_usd_per_ai_call

    def _weekly_floor(self) -> float | None:
        if not self._weekly_enabled() or self._week_baseline is None:
            return None
        return self._week_baseline * (1.0 - float(self.settings.weekly_loss_pct or 0.0))

    def update(self, *, equity: float, unrealized_pnl: float) -> RegimeState:
        self._roll_day()
        self._roll_week(equity)
        s = self.settings
        start = s.paper_starting_bankroll
        burn = self.session_ai_cost_usd
        kill_floor = start * (1.0 - s.kill_floor_pct)
        profit = max(0.0, equity - start)
        # Cushion stays configurable. Zero cushion is `burn >= profit`, but a
        # zero burn must not DIE when profit is also zero.
        die_burn_line = profit + s.api_die_cushion_usd

        weekly_floor = self._weekly_floor()
        weekly_breach = weekly_floor is not None and equity <= weekly_floor
        if weekly_breach:
            self._halt(
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f} "
                f"baseline={self._week_baseline:.2f}"
            )

        def finish(mode: str, reason: str) -> RegimeState:
            st = RegimeState(mode, reason, self.session_ai_calls, burn, equity, start)
            self.last = st
            return st

        if weekly_breach and not s.regime_enabled:
            return finish(
                "DIE",
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f}",
            )

        if not s.regime_enabled:
            return finish("ATTACK", "regime_disabled")

        if equity <= kill_floor:
            return finish(
                "DIE",
                f"kill_floor equity={equity:.2f}<={kill_floor:.2f}",
            )
        if weekly_breach:
            return finish(
                "DIE",
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f} "
                f"baseline={self._week_baseline:.2f}",
            )
        if burn > 0 and burn >= die_burn_line:
            return finish(
                "DIE",
                f"api_burn {burn:.4f}>={die_burn_line:.4f}",
            )

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

        return finish("DEFEND" if defend else "ATTACK", reason)

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
