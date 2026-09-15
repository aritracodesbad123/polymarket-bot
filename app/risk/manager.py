from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
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
    def __init__(self, settings: Settings, repo: Repositories) -> None:
        self.settings = settings
        self.repo = repo
        self.kill = KillSwitch(repo)
        self._api_failures = 0
        self._stale_hits = 0
        self.daily_pnl = 0.0
        self.peak_equity: float | None = None

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
