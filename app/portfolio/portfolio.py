from __future__ import annotations

from app.broker.paper import PaperBroker
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
