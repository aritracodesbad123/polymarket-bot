from __future__ import annotations

from app.broker.paper import PaperBroker, PaperPosition
from app.storage.repositories import Repositories


class Portfolio:
    def __init__(self, paper: PaperBroker, repo: Repositories) -> None:
        self.paper = paper
        self.repo = repo

    def snapshot(self, marks: dict[str, float] | None = None) -> dict:
        marks = marks or {}
        unreal = 0.0
        for p in self.paper._positions.values():
            m = marks.get(p.token_id, p.avg_price)
            unreal += p.shares * (m - p.avg_price)
        snap = {
            "cash": self.paper.cash,
            "reserved_cash": self.paper.reserved,
            "equity": self.paper.equity(marks),
            "exposure": self.paper.exposure(marks),
            "realized_pnl": self.paper.realized_pnl,
            "unrealized_pnl": unreal,
        }
        self.repo.insert_portfolio(snap)
        for p in self.paper._positions.values():
            if p.shares > 0:
                self.repo.upsert_position(
                    {
                        "token_id": p.token_id,
                        "market_id": p.market_id,
                        "shares": p.shares,
                        "avg_price": p.avg_price,
                        "realized_pnl": p.realized_pnl,
                        "category": p.category,
                        "correlation_group": p.correlation_group,
                    }
                )
        return snap

    def hydrate_paper(self, starting_bankroll: float) -> None:
        """Restore cash and open positions from the latest snapshot.

        No snapshot leaves the broker at ``starting_bankroll`` (new database).
        Does not write. Positions are loaded only together with that snapshot's
        cash so equity is not double-counted.
        """
        if starting_bankroll < 0:
            raise ValueError("starting_bankroll")
        snap = self.repo.latest_portfolio()
        if snap is None:
            return
        self.paper.cash = float(snap["cash"])
        self.paper.reserved = float(snap["reserved_cash"] or 0.0)
        self.paper.realized_pnl = float(snap["realized_pnl"] or 0.0)
        self.paper._positions.clear()
        for row in self.repo.positions():
            token = row["token_id"]
            self.paper._positions[token] = PaperPosition(
                token_id=token,
                market_id=row["market_id"] or "",
                shares=float(row["shares"]),
                avg_price=float(row["avg_price"]),
                realized_pnl=float(row["realized_pnl"] or 0.0),
                category=row["category"] or "",
                correlation_group=row["correlation_group"] or "",
            )
