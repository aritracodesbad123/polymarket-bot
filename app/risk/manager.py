from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.config import Settings
from app.risk.caps import daily_loss_cap_usd, position_notional_cap, total_exposure_cap
from app.risk.regime import utc_day
from app.storage.db import DatabaseError
from app.storage.repositories import Repositories


@dataclass
class Exposure:
    by_market: dict[str, float] = field(default_factory=dict)
    by_category: dict[str, float] = field(default_factory=dict)
    by_group: dict[str, float] = field(default_factory=dict)
    total: float = 0.0


def exposure_from_positions(positions: list, marks: dict[str, float]) -> Exposure:
    exp = Exposure()
    for p in positions:
        token = p["token_id"] if not isinstance(p, dict) else p["token_id"]
        shares = float(p["shares"])
        avg = float(p["avg_price"])
        mark = marks.get(token, avg)
        usd = shares * mark
        market_id = p["market_id"] or token
        cat = p["category"] or "other"
        grp = p["correlation_group"] or market_id
        exp.by_market[market_id] = exp.by_market.get(market_id, 0.0) + usd
        exp.by_category[cat] = exp.by_category.get(cat, 0.0) + usd
        exp.by_group[grp] = exp.by_group.get(grp, 0.0) + usd
        exp.total += usd
    return exp


class KillSwitch:
    def __init__(self, repo: Repositories) -> None:
        self.repo = repo

    def trigger(self, reason: str) -> None:
        self.repo.halt(reason)

    def active(self) -> bool:
        return self.repo.state().halted


class RiskManager:
    def __init__(
        self,
        settings: Settings,
        repo: Repositories,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.kill = KillSwitch(repo)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._api_failures = 0
        self._stale_hits = 0
        self.daily_pnl = 0.0
        self.daily_realized_pnl = 0.0
        self._daily_day = utc_day(self._now()).isoformat()
        self.peak_equity: float | None = None
        self._load_daily_realized()

    def note_api_failure(self) -> None:
        self._api_failures += 1
        if self._api_failures >= 8:
            self.kill.trigger("repeated_api_failures")

    def note_api_ok(self) -> None:
        self._api_failures = 0

    def note_stale(self) -> None:
        self._stale_hits += 1
        if self._stale_hits >= 20:
            self.kill.trigger("repeated_stale_data")

    def note_book_fresh(self) -> None:
        """Reset stale counter after a book that clears freshness gates."""
        self._stale_hits = 0

    def note_ai_error(self, n: int) -> None:
        if n >= 10:
            self.kill.trigger("repeated_ai_errors")

    def note_duplicate_anomaly(self) -> None:
        self.kill.trigger("suspicious_duplicate_order")

    def note_recon_mismatch(self, detail: str) -> None:
        self.kill.trigger(f"reconciliation_mismatch:{detail}")

    def _today(self) -> str:
        return utc_day(self._now()).isoformat()

    def _load_daily_realized(self) -> None:
        try:
            day, pnl = self.repo.daily_realized_state()
        except DatabaseError:
            raise
        today = self._today()
        if day is None:
            if pnl != 0:
                raise DatabaseError("daily_realized_without_day")
            self._daily_day = today
            self.daily_realized_pnl = 0.0
            return
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise DatabaseError("daily_realized_day_invalid") from exc
        today_d = date.fromisoformat(today)
        if parsed > today_d:
            raise DatabaseError("daily_realized_day_in_future")
        if parsed < today_d:
            self._daily_day = today
            self.daily_realized_pnl = 0.0
            self._persist_daily()
        else:
            self._daily_day = today
            self.daily_realized_pnl = pnl
        self.enforce_daily_realized_cap()

    def _roll_daily(self) -> None:
        today = self._today()
        if today == self._daily_day:
            return
        self._daily_day = today
        self.daily_realized_pnl = 0.0
        self._persist_daily()

    def _persist_daily(self) -> None:
        try:
            self.repo.set_daily_realized(self._daily_day, self.daily_realized_pnl)
        except DatabaseError:
            try:
                if not self.repo.state().halted:
                    self.repo.halt("daily_realized_persist_failed")
            except DatabaseError:
                pass
            raise

    def note_realized_pnl(self, delta: float) -> None:
        """Add day-scoped realized P&L and enforce the absolute/percentage cap."""
        self._roll_daily()
        self.daily_realized_pnl += delta
        self._persist_daily()
        self.enforce_daily_realized_cap()

    def enforce_daily_realized_cap(self) -> None:
        """Halt when today's realized loss reaches the stricter configured cap.

        No-op when MAX_DAILY_LOSS_USD is unset. Does not clear an existing halt
        and does not replace the percentage equity check in note_equity.
        """
        self._roll_daily()
        if self.repo.state().halted:
            return
        limit = daily_loss_cap_usd(self.settings, self.settings.paper_starting_bankroll)
        if limit is None:
            return
        loss = -self.daily_realized_pnl
        if self.daily_realized_pnl < 0 and loss >= limit:
            self.kill.trigger(f"daily_realized_loss_cap:{loss:.4f}>={limit:.4f}")

    def order_block_reason(
        self,
        *,
        size_usd: float,
        existing_total_exposure: float,
        bankroll: float,
    ) -> str | None:
        """Execution-path refusal. Stricter of percentage and absolute caps."""
        if self.repo.state().halted:
            return "halted"
        pos_cap = position_notional_cap(self.settings, bankroll)
        if size_usd > pos_cap + 1e-6:
            return "position_usd_cap"
        exp_cap = total_exposure_cap(self.settings, bankroll)
        if existing_total_exposure + size_usd > exp_cap + 1e-6:
            return "exposure_usd_cap"
        return None

    def note_equity(self, equity: float, start_bankroll: float) -> None:
        if self.peak_equity is None:
            self.peak_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0
        if dd >= 0.20:
            self.kill.trigger(f"severe_drawdown:{dd:.3f}")
        if equity <= start_bankroll * (1 - self.settings.max_daily_loss_pct) and self.daily_pnl < 0:
            # daily loss approximated vs start if no session mark
            if abs(self.daily_pnl) >= start_bankroll * self.settings.max_daily_loss_pct:
                self.kill.trigger("daily_loss_limit")

    def note_loss(self) -> None:
        st = self.repo.state()
        n = st.consecutive_losses + 1
        self.repo.set_consecutive_losses(n)
        if n >= self.settings.max_consecutive_losses:
            self.kill.trigger(f"consecutive_losses:{n}")

    def note_win(self) -> None:
        self.repo.set_consecutive_losses(0)
