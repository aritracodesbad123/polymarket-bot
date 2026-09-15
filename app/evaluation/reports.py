from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.evaluation.calibration import brier, buckets, log_loss
from app.risk.authorization import PAPER_MIN_SECONDS, auth_from_state
from app.storage.repositories import Repositories

REPORTS = Path(__file__).resolve().parent.parent.parent / "reports"


def _md(path: Path, body: str) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def daily_report(settings: Settings, repo: Repositories, extra: dict) -> str:
    st = repo.state()
    counts = repo.counts()
    events = repo.event_counts()
    lines = [
        f"# POLYGROK daily report",
        f"MODE: {'HALTED' if st.halted else st.trading_mode.upper()}",
        f"bankroll: {extra.get('equity')}",
        f"cash: {extra.get('cash')}",
        f"reserved cash: {extra.get('reserved')}",
        f"open positions: {extra.get('open_positions')}",
        f"open orders: {extra.get('open_orders')}",
        f"exposure: {extra.get('exposure')}",
        f"daily P&L: {extra.get('daily_pnl')}",
        f"cumulative P&L: {extra.get('cum_pnl')}",
        f"drawdown: {extra.get('drawdown')}",
        f"markets scanned: {counts['markets']}",
        f"Grok calls: {counts['predictions']}",
        f"candidate count: {extra.get('candidates')}",
        f"trades: {counts['orders']}",
        f"rejected trades: {extra.get('rejected')}",
        f"average edge: {extra.get('avg_edge')}",
        f"execution-adjusted edge: {extra.get('avg_exec_edge')}",
        f"slippage: {extra.get('avg_slip')}",
        f"fills: {counts['fills']}",
        f"partial fills: {extra.get('partials')}",
        f"API errors: {events.get('API_ERROR', 0)}",
        f"AI errors: {events.get('GROK_ERROR', 0)}",
    ]
    body = "\n".join(lines) + "\n"
    _md(REPORTS / "daily.md", body)
    return body


def calibration_report(repo: Repositories) -> str:
    resolved = repo.resolved()
    preds = repo.predictions()
    by_id = {r["market_id"]: r for r in resolved}
    pairs: list[tuple[float, int]] = []
    for p in preds:
        r = by_id.get(p["market_id"])
        if r is None or p["estimated_probability"] is None:
            continue
        y = 1 if str(r["outcome"]).upper() in ("YES", "1") else 0
        pairs.append((float(p["estimated_probability"]), y))
    lines = ["# Calibration", f"n={len(pairs)}"]
    if pairs:
        lines.append(f"Brier: {sum(brier(p,y) for p,y in pairs)/len(pairs):.4f}")
        lines.append(f"Log loss: {sum(log_loss(p,y) for p,y in pairs)/len(pairs):.4f}")
        lines.append("| bucket | n | predicted | actual | error |")
        lines.append("|---|---|---|---|---|")
        for b in buckets(pairs):
            lines.append(
                f"| {b['bucket']} | {b['n']} | {b['predicted']:.3f} | {b['actual']:.3f} | {b['calibration_error']:.3f} |"
            )
    body = "\n".join(lines) + "\n"
    _md(REPORTS / "calibration.md", body)
    return body


def pre_live_report(settings: Settings, repo: Repositories, extra: dict) -> tuple[str, str]:
    st = repo.state()
    counts = repo.counts()
    events = repo.event_counts()
    started = st.paper_trading_started_at or ""
    body = "\n".join(
        [
            "# Pre-live report",
            f"paper_trading_started_at: {started}",
            f"total markets scanned: {counts['markets']}",
            f"total AI decisions: {counts['predictions']}",
            f"total paper trades: {counts['orders']}",
            f"resolved trades: {len(repo.resolved())}",
            f"paper P&L: {extra.get('cum_pnl')}",
            f"ROI: {extra.get('roi')}",
            f"max drawdown: {extra.get('drawdown')}",
            f"average edge: {extra.get('avg_edge')}",
            f"execution-adjusted edge: {extra.get('avg_exec_edge')}",
            f"simulated slippage: {extra.get('avg_slip')}",
            f"partial fills: {extra.get('partials')}",
            f"API errors: {events.get('API_ERROR', 0)}",
            f"AI errors: {events.get('GROK_ERROR', 0)}",
            f"stale data incidents: {events.get('TRADE_REJECTED', 0)}",
            f"rejected trades: {extra.get('rejected')}",
            f"largest position: {extra.get('largest_position')}",
            f"maximum exposure: {extra.get('max_exposure')}",
            f"strategy version: {settings.strategy_version}",
            f"prompt version: {settings.prompt_version}",
            "",
            "This report does not activate live trading.",
        ]
    ) + "\n"
    path = _md(REPORTS / "pre_live_report.md", body)
    h = hashlib.sha256(body.encode()).hexdigest()
    return str(path), h
