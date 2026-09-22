"""ATTACK / DEFEND / DIE — pay for yourself or stop burning capital and API.

Day-scoped AI burn and the weekly equity baseline live in system_state.
A restart must not zero the cost-versus-return controls. If that state
cannot be read or written, the engine fails closed.

Session spend is AI_SESSION_BUDGET_USD, not API_DIE_CUSHION_USD.
No fills: burn >= budget stops new screening (DIE, no halt).
Fills or open positions (legacy, auto-switch off): stop new screening when
unrealized PnL < burn.
With ESTIMATOR=microstructure and ESTIMATOR_AUTO_SWITCH (default ON): after
fills, LLM↔micro flips on daily realized PnL vs burn (see _spend_stop).
Holding review and exits are not part of this gate.
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
    screening_allowed: bool = True
    has_taken_fills: bool = False


def new_screening_allowed(state: RegimeState) -> bool:
    """New universe scan and AI calls. Holdings are a separate path."""
    return bool(state.screening_allowed) and state.mode != "DIE"


def ai_spend_blocked_screening(state: RegimeState) -> bool:
    """True when session AI spend stopped new screening.

    Covers the no-fill DIE (``ai_session_budget``) and the post-fill
    ``screening_stop``. Kill floor and the weekly equity stop stay blocked
    even if a non-LLM estimator is armed.
    """
    if state.screening_allowed and state.mode != "DIE":
        return False
    reason = state.reason or ""
    if reason.startswith("kill_floor") or reason.startswith("weekly_equity_stop"):
        return False
    return reason.startswith("ai_session_budget") or reason.startswith("screening_stop")


def estimator_auto_switch_armed(settings: Settings) -> bool:
    """Realized↔burn LLM/micro flip. Only with ESTIMATOR=microstructure."""
    name = (settings.estimator or "").strip().lower()
    if name != "microstructure":
        return False
    return bool(settings.estimator_auto_switch)


def screening_path(state: RegimeState, settings: Settings, *, force_micro: bool = False) -> str:
    """Return ``llm``, ``micro``, or ``none`` for new screening only."""
    if force_micro:
        return "micro"
    name = (settings.estimator or "").strip().lower()
    micro_armed = name == "microstructure"
    if new_screening_allowed(state):
        return "llm"
    if micro_armed and ai_spend_blocked_screening(state):
        return "micro"
    return "none"


def estimator_switch_reason(state: RegimeState, path: str) -> str:
    """Stable reason token for ESTIMATOR_SWITCH logs."""
    if path == "llm":
        if state.session_ai_cost_usd <= 0:
            return "zero_burn"
        return "pnl_gt_burn"
    if path == "none":
        reason = state.reason or ""
        if reason.startswith("kill_floor"):
            return "kill_floor"
        if reason.startswith("weekly_equity_stop"):
            return "weekly_equity_stop"
        return "screening_blocked"
    reason = state.reason or ""
    if reason.startswith("ai_session_budget"):
        return "ai_session_budget"
    if "burn_exhausted" in reason:
        return "burn_exhausted"
    if "realized_unknown" in reason:
        return "realized_unknown"
    if "realized_eq_burn" in reason:
        return "realized_eq_burn"
    if "realized_lt_burn" in reason:
        return "realized_lt_burn"
    if reason.startswith("screening_stop"):
        return "screening_stop"
    return "micro"


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
            raise DatabaseError("ai_burn_count_negative")
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

    def _resolve_fills(self, has_taken_fills: bool | None) -> bool:
        if has_taken_fills is not None:
            return bool(has_taken_fills)
        if self.repo is None:
            return False
        return self.repo.cohort_has_taken_fills()

    def _defend_reason(
        self, *, equity: float, unrealized_pnl: float, burn: float, start: float
    ) -> str | None:
        """Cushion is a DEFEND band. Zero turns the band off; it is not a budget."""
        cushion = self.settings.api_die_cushion_usd
        if cushion <= 0:
            return None
        profit = max(0.0, equity - start)
        cushion_line = profit + cushion
        if equity < start - cushion:
            return f"equity_below_start {equity:.2f}<{start:.2f}"
        if unrealized_pnl < -cushion:
            return f"unrealized {unrealized_pnl:.4f}"
        if burn >= cushion_line * 0.5 and cushion_line > 0:
            return f"burn_half_cushion {burn:.4f}"
        return None

    def _spend_stop(
        self,
        *,
        burn: float,
        unrealized_pnl: float,
        has_fills: bool,
        realized_pnl: float | None,
    ) -> tuple[str, str] | None:
        """Screening stop from session AI spend.

        No fills: hard budget, reported as DIE. Does not halt the bot.
        Fills exist (legacy): stop screening when unrealized PnL is under burn.
        Auto-switch (ESTIMATOR=microstructure + ESTIMATOR_AUTO_SWITCH): after
        fills, block LLM when realized <= burn or burn >= budget so micro can
        run; re-allow LLM when realized > burn and burn < budget.
        Zero burn never stops screening (startup and UTC-day rollover).
        Unknown realized with auto-switch armed fails closed (block LLM).
        """
        if burn <= 0:
            return None
        budget = self.settings.ai_session_budget_usd
        if estimator_auto_switch_armed(self.settings):
            if realized_pnl is None:
                if not has_fills and budget > 0 and burn >= budget:
                    return ("DIE", f"ai_session_budget {burn:.4f}>={budget:.4f}")
                return ("SCREEN", "screening_stop realized_unknown")
            if budget > 0 and burn >= budget:
                if not has_fills:
                    return ("DIE", f"ai_session_budget {burn:.4f}>={budget:.4f}")
                return (
                    "SCREEN",
                    f"screening_stop burn_exhausted {burn:.4f}>={budget:.4f}",
                )
            if not has_fills:
                # Pre-fill hunt stays on LLM until the hard budget DIE.
                return None
            if realized_pnl > burn:
                return None
            if realized_pnl < burn:
                return (
                    "SCREEN",
                    f"screening_stop realized_lt_burn "
                    f"{realized_pnl:.4f}<burn {burn:.4f}",
                )
            return (
                "SCREEN",
                f"screening_stop realized_eq_burn "
                f"{realized_pnl:.4f}<={burn:.4f}",
            )

        if not has_fills:
            if budget > 0 and burn >= budget:
                return ("DIE", f"ai_session_budget {burn:.4f}>={budget:.4f}")
            return None
        if unrealized_pnl < burn:
            return (
                "SCREEN",
                f"screening_stop unrealized {unrealized_pnl:.4f}<burn {burn:.4f}",
            )
        return None

    def update(
        self,
        *,
        equity: float,
        unrealized_pnl: float,
        has_taken_fills: bool | None = None,
        realized_pnl: float | None = None,
    ) -> RegimeState:
        self._roll_day()
        self._roll_week(equity)
        s = self.settings
        start = s.paper_starting_bankroll
        burn = self.session_ai_cost_usd
        kill_floor = start * (1.0 - s.kill_floor_pct)
        has_fills = self._resolve_fills(has_taken_fills)

        weekly_floor = self._weekly_floor()
        weekly_breach = weekly_floor is not None and equity <= weekly_floor
        if weekly_breach:
            self._halt(
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f} "
                f"baseline={self._week_baseline:.2f}"
            )

        def finish(
            mode: str,
            reason: str,
            *,
            screening_allowed: bool,
        ) -> RegimeState:
            st = RegimeState(
                mode,
                reason,
                self.session_ai_calls,
                burn,
                equity,
                start,
                screening_allowed=screening_allowed,
                has_taken_fills=has_fills,
            )
            self.last = st
            return st

        if weekly_breach and not s.regime_enabled:
            return finish(
                "DIE",
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f}",
                screening_allowed=False,
            )

        if not s.regime_enabled:
            return finish("ATTACK", "regime_disabled", screening_allowed=True)

        if equity <= kill_floor:
            return finish(
                "DIE",
                f"kill_floor equity={equity:.2f}<={kill_floor:.2f}",
                screening_allowed=False,
            )
        if weekly_breach:
            return finish(
                "DIE",
                f"weekly_equity_stop equity={equity:.2f}<={weekly_floor:.2f} "
                f"baseline={self._week_baseline:.2f}",
                screening_allowed=False,
            )

        spend = self._spend_stop(
            burn=burn,
            unrealized_pnl=unrealized_pnl,
            has_fills=has_fills,
            realized_pnl=realized_pnl,
        )
        defend_reason = self._defend_reason(
            equity=equity, unrealized_pnl=unrealized_pnl, burn=burn, start=start
        )
        if spend is not None and spend[0] == "DIE":
            return finish("DIE", spend[1], screening_allowed=False)
        if spend is not None:
            mode = "DEFEND" if defend_reason else "ATTACK"
            reason = spend[1] if not defend_reason else f"{spend[1]}; {defend_reason}"
            return finish(mode, reason, screening_allowed=False)

        return finish(
            "DEFEND" if defend_reason else "ATTACK",
            defend_reason or "healthy",
            screening_allowed=True,
        )

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
        if self.last is not None and not self.last.screening_allowed:
            return 0
        if m == "DEFEND":
            return min(self.settings.max_grok_calls_per_cycle, self.settings.defend_max_grok_calls)
        return self.settings.max_grok_calls_per_cycle
