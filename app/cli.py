"""python -m app.cli <command>"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timezone

from app.config import Settings
from app.evaluation.reports import calibration_report, daily_report, pre_live_report
from app.main import BANNER_HALTED, BANNER_LIVE, BANNER_PAPER, TradingApp, banner_for, run
from app.risk.authorization import LIVE_CONFIRMATION_PHRASE, PAPER_MIN_SECONDS, parse_ts


def _app(settings: Settings | None = None) -> TradingApp:
    return TradingApp(settings or Settings.from_env())


def cmd_status(app: TradingApp) -> int:
    print(banner_for(app.settings, app.repo))
    st = app.repo.state()
    print(f"paper_trading_started_at: {st.paper_trading_started_at}")
    print(f"halted: {st.halted} {st.halt_reason or ''}")
    print(f"live_activated_at: {st.live_activated_at}")
    auth = app.live.authorize_now()
    print(f"live_eligible: {auth.eligible}")
    print(f"live_allowed: {auth.allowed}")
    if auth.reasons:
        print("live_blockers: " + ", ".join(auth.reasons))
    print(f"cash: {app.paper.cash:.2f} reserved: {app.paper.reserved:.2f}")
    print(f"equity: {app.paper.equity():.2f}")
    return 0


async def cmd_markets(app: TradingApp) -> int:
    rows = await app.scanner.scan()
    for m, reason in rows:
        flag = "OK" if reason is None else reason
        print(f"{flag:24} {m.market_id}  {m.question[:80]}")
    return 0


def cmd_opportunities(app: TradingApp) -> int:
    for r in app.repo.decisions(20):
        print(
            f"{r['ts']} approved={r['approved']} {r['reject_reason'] or ''} "
            f"{r['market_id']} edge={r['raw_edge']}"
        )
    return 0


def cmd_positions(app: TradingApp) -> int:
    for p in app.paper._positions.values():
        print(f"{p.token_id[:16]} shares={p.shares:.4f} avg={p.avg_price:.4f} {p.market_id}")
    return 0


def cmd_orders(app: TradingApp) -> int:
    for r in app.repo.orders(30):
        print(f"{r['created_at']} {r['status']} {r['side']} {r['token_id']} {r['size_shares']}@{r['price']}")
    return 0


def cmd_daily(app: TradingApp) -> int:
    body = daily_report(app.settings, app.repo, app.extra_stats())
    print(body)
    app.repo.event("DAILY_REPORT", "written")
    return 0


def cmd_calibration(app: TradingApp) -> int:
    print(calibration_report(app.repo))
    return 0


def cmd_pre_live(app: TradingApp) -> int:
    path, _h = pre_live_report(app.settings, app.repo, app.extra_stats())
    st = app.repo.state()
    started = parse_ts(st.paper_trading_started_at)
    if started:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed >= PAPER_MIN_SECONDS:
            print("LIVE ACTIVATION ELIGIBLE")
        else:
            print(f"paper days remaining: {(PAPER_MIN_SECONDS - elapsed)/86400:.2f}")
    else:
        print("paper clock not started")
    print(f"wrote {path}")
    print("Day 7 does not activate live trading.")
    return 0


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def cmd_request_live(app: TradingApp, confirmation: str | None) -> int:
    cmd_pre_live(app)
    print("Risk summary:")
    print(f"  MIN_EDGE={app.settings.min_edge} KELLY={app.settings.kelly_multiplier}")
    print(
        f"  canary order ${app.settings.canary_max_order_usd} "
        f"daily ${app.settings.canary_max_daily_notional_usd} "
        f"positions {app.settings.canary_max_open_positions}"
    )
    if confirmation is None:
        confirmation = input(f'Type "{LIVE_CONFIRMATION_PHRASE}" to record activation:\n')
    if confirmation != LIVE_CONFIRMATION_PHRASE:
        print("confirmation rejected")
        return 1
    st = app.repo.state()
    started = parse_ts(st.paper_trading_started_at)
    dur = (datetime.now(timezone.utc) - started).total_seconds() if started else 0.0
    if started is None or dur < PAPER_MIN_SECONDS:
        print("activation refused: 7 full paper days have not elapsed")
        return 1
    _path, report_hash = pre_live_report(app.settings, app.repo, app.extra_stats())
    app.repo.insert_activation(
        {
            "operator_confirmation": confirmation,
            "paper_duration_seconds": dur,
            "git_commit": _git_commit(),
            "config_hash": app.settings.config_hash(),
            "strategy_version": app.settings.strategy_version,
            "prompt_version": app.settings.prompt_version,
            "risk_config": app.settings.public_dict(),
            "report_hash": report_hash,
        }
    )
    auth = app.live.authorize_now()
    if auth.allowed:
        now = datetime.now(timezone.utc).isoformat()
        app.repo.set_live_activated(now)
        app.repo.event("LIVE_MODE", "canary unlocked")
        print(BANNER_LIVE)
        print("Live canary unlocked. Bot will trade autonomously inside canary limits.")
        return 0
    print("Activation recorded. Live still locked:")
    print(", ".join(auth.reasons))
    print("Set TRADING_MODE=live and LIVE_TRADING_ENABLED=true after 7 full paper days.")
    return 0


def cmd_kill(app: TradingApp) -> int:
    app.risk.kill.trigger("operator_kill")
    print(BANNER_HALTED)
    print("halted. new orders stopped.")
    return 0


def cmd_resume_paper(app: TradingApp) -> int:
    app.repo.resume_paper()
    print(BANNER_PAPER)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("markets")
    sub.add_parser("opportunities")
    sub.add_parser("positions")
    sub.add_parser("orders")
    sub.add_parser("daily-report")
    sub.add_parser("calibration-report")
    sub.add_parser("pre-live-report")
    act = sub.add_parser("request-live-activation")
    act.add_argument("--confirmation", default=None)
    sub.add_parser("kill")
    sub.add_parser("resume-paper")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.cmd == "run":
        run()
        return 0
    app = _app(settings)
    if args.cmd == "status":
        return cmd_status(app)
    if args.cmd == "markets":
        return asyncio.run(cmd_markets(app))
    if args.cmd == "opportunities":
        return cmd_opportunities(app)
    if args.cmd == "positions":
        return cmd_positions(app)
    if args.cmd == "orders":
        return cmd_orders(app)
    if args.cmd == "daily-report":
        return cmd_daily(app)
    if args.cmd == "calibration-report":
        return cmd_calibration(app)
    if args.cmd == "pre-live-report":
        return cmd_pre_live(app)
    if args.cmd == "request-live-activation":
        return cmd_request_live(app, args.confirmation)
    if args.cmd == "kill":
        return cmd_kill(app)
    if args.cmd == "resume-paper":
        return cmd_resume_paper(app)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
