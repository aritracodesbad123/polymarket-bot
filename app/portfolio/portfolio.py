from __future__ import annotations

import json

from app.broker.paper import FLAT_SHARES, PaperBroker, PaperPosition
from app.storage.repositories import Repositories


def _saved_marks(snap) -> tuple[dict[str, float], set[str]]:
    """Last marks and book_gone tokens stored on the portfolio snapshot."""
    keys = snap.keys() if hasattr(snap, "keys") else ()
    raw = snap["extra_json"] if "extra_json" in keys else None
    if not raw:
        return {}, set()
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}, set()
    if not isinstance(payload, dict):
        return {}, set()
    marks = payload.get("marks") or {}
    if not isinstance(marks, dict):
        marks = {}
    gone_raw = payload.get("book_gone") or []
    gone = {str(t) for t in gone_raw} if isinstance(gone_raw, list) else set()
    return {str(k): float(v) for k, v in marks.items() if v is not None}, gone


class Portfolio:
    def __init__(self, paper: PaperBroker, repo: Repositories) -> None:
        self.paper = paper
        self.repo = repo

    def snapshot(self, marks: dict[str, float] | None = None) -> dict:
        marks = marks or {}
        for p in self.paper._positions.values():
            if p.token_id not in marks and p.last_mark is not None:
                marks[p.token_id] = float(p.last_mark)
        for tid, p in list(self.paper._positions.items()):
            if p.shares <= FLAT_SHARES:
                del self.paper._positions[tid]
        unreal = 0.0
        open_rows: list[dict] = []
        for p in self.paper._positions.values():
            if p.shares <= FLAT_SHARES:
                continue
            if p.token_id in marks:
                m = marks[p.token_id]
            elif p.last_mark is not None:
                m = p.last_mark
            else:
                m = p.avg_price
            unreal += p.shares * (m - p.avg_price)
            open_rows.append(
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
        kept_marks: dict[str, float] = {}
        gone: list[str] = []
        for p in self.paper._positions.values():
            if p.shares <= FLAT_SHARES:
                continue
            if p.token_id in marks:
                p.last_mark = float(marks[p.token_id])
            if p.last_mark is not None:
                kept_marks[p.token_id] = float(p.last_mark)
            if p.book_gone:
                gone.append(p.token_id)
        snap = {
            "cash": self.paper.cash,
            "reserved_cash": self.paper.reserved,
            "equity": self.paper.equity(marks),
            "exposure": self.paper.exposure(marks),
            "realized_pnl": self.paper.realized_pnl,
            "unrealized_pnl": unreal,
            "extra": {"marks": kept_marks, "book_gone": gone},
        }
        self.repo.persist_open_book(snap, open_rows)
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
        saved_marks, gone = _saved_marks(snap)
        self.paper._positions.clear()
        for row in self.repo.positions():
            shares = float(row["shares"] or 0.0)
            if shares <= FLAT_SHARES:
                continue
            token = row["token_id"]
            raw_mark = saved_marks.get(token)
            self.paper._positions[token] = PaperPosition(
                token_id=token,
                market_id=row["market_id"] or "",
                shares=shares,
                avg_price=float(row["avg_price"]),
                realized_pnl=float(row["realized_pnl"] or 0.0),
                category=row["category"] or "",
                correlation_group=row["correlation_group"] or "",
                book_gone=token in gone,
                last_mark=None if raw_mark is None else float(raw_mark),
            )
