#!/usr/bin/env python3
"""POLYGROK realtime ops console — reads LaunchAgent logs + polygrok.db on this Mac."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

HOME = Path.home()
# ops-console/ lives inside the bot repo
BOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BOT_DIR / "polygrok-week2-5000.db"
LOG_DIR = HOME / "Library" / "Application Support" / "com.polygrok.bot"
OUT_LOG = LOG_DIR / "launchd.out.log"
ERR_LOG = LOG_DIR / "launchd.err.log"
AGENT_LABEL = "com.polygrok.bot"
# Week-2 $5k cohort
START_BANKROLL = 5000.0
KILL_FLOOR = 4500.0
WEEKLY_STOP = 4750.0
DAILY_LOSS_BUDGET = 50.0
AI_SESSION_BUDGET_USD = 10.0
ESTIMATED_USD_PER_AI_CALL = 0.02  # estimated only
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
# Share marks across overlapping polls so the console doesn't stall.
_MARKS_TTL_S = 3.0
_EQUITY_RING: deque[dict[str, float | str]] = deque(maxlen=450)  # ~15m @ 2s
_MARKS_CACHE: dict[str, Any] = {"t": 0.0, "by_token": {}, "ok": False, "err": None}
_MARKS_LOCK = threading.Lock()
_MARKS_FETCHING = False

app = FastAPI(title="POLYGROK Ops Console")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


@contextmanager
def connect() -> sqlite3.Connection:
    """Always close — plain `with sqlite3.connect` leaks FDs on this Mac."""
    # Read-only + busy timeout so bot writers can't kill /api/snapshot mid-poll.
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=30000")
        yield con
    finally:
        con.close()


def _book_level_px(level: Any) -> float | None:
    try:
        if isinstance(level, dict):
            return float(level.get("price") or level.get("p") or 0)
        if isinstance(level, (list, tuple)) and level:
            return float(level[0])
        return float(level)
    except Exception:
        return None


def _clob_mid(token_id: str) -> tuple[float | None, float | None, float | None]:
    """Return (best_bid, best_ask, mid) from CLOB REST. Needs a UA or CF 403s."""
    req = urllib.request.Request(
        f"{CLOB_BOOK_URL}?token_id={token_id}",
        headers={
            "User-Agent": "polygrok-ops-console/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        data = json.loads(resp.read().decode())
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid = max((p for p in (_book_level_px(x) for x in bids) if p is not None), default=None)
    ask = min((p for p in (_book_level_px(x) for x in asks) if p is not None), default=None)
    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    elif ask is not None:
        mid = ask
    elif bid is not None:
        mid = bid
    return bid, ask, mid


def live_marks_by_token(con: sqlite3.Connection) -> dict[str, Any]:
    """CLOB mids for open positions. Cached + single-flight so polls don't stack."""
    global _MARKS_FETCHING
    now = time.time()
    if now - float(_MARKS_CACHE["t"]) < _MARKS_TTL_S and _MARKS_CACHE["by_token"]:
        return dict(_MARKS_CACHE)
    with _MARKS_LOCK:
        # Another request already refreshing — serve last good marks.
        if _MARKS_FETCHING and _MARKS_CACHE.get("by_token"):
            return dict(_MARKS_CACHE)
        if now - float(_MARKS_CACHE["t"]) < _MARKS_TTL_S and _MARKS_CACHE["by_token"]:
            return dict(_MARKS_CACHE)
        _MARKS_FETCHING = True
    try:
        if not _has_positions_cols(con):
            with _MARKS_LOCK:
                _MARKS_CACHE.update({"t": time.time(), "by_token": {}, "ok": False, "err": "no positions"})
                return dict(_MARKS_CACHE)
        rows = con.execute(
            "SELECT token_id, market_id, shares, avg_price FROM positions WHERE shares != 0"
        ).fetchall()
        by_token: dict[str, dict[str, Any]] = {}
        err: str | None = None
        ok_n = 0
        if rows:
            with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
                futs = {pool.submit(_clob_mid, str(r["token_id"])): r for r in rows}
                for fut in as_completed(futs):
                    r = futs[fut]
                    tid = str(r["token_id"])
                    entry = float(r["avg_price"] or 0)
                    shares = float(r["shares"] or 0)
                    bid = ask = mid = None
                    try:
                        bid, ask, mid = fut.result()
                        if mid is not None:
                            ok_n += 1
                    except Exception as exc:
                        err = str(exc)
                        mid = None
                    mark = float(mid) if mid is not None else entry
                    by_token[tid] = {
                        "token_id": tid,
                        "market_id": r["market_id"],
                        "shares": shares,
                        "entry": entry,
                        "mark": mark,
                        "bid": bid,
                        "ask": ask,
                        "live_book": mid is not None,
                        "unrealized": (mark - entry) * shares,
                        "notional": shares * mark,
                    }
        with _MARKS_LOCK:
            _MARKS_CACHE.update(
                {
                    "t": time.time(),
                    "by_token": by_token,
                    "ok": ok_n > 0,
                    "err": None if ok_n else err,
                    "fetched": ok_n,
                    "open": len(rows),
                }
            )
            return dict(_MARKS_CACHE)
    finally:
        with _MARKS_LOCK:
            _MARKS_FETCHING = False


def agent_status() -> dict[str, Any]:
    uid = os.getuid()
    running = False
    pid = None
    etime = None
    try:
        out = subprocess.check_output(
            ["launchctl", "print", f"gui/{uid}/{AGENT_LABEL}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        running = "state = running" in out
        m = re.search(r"\bpid = (\d+)", out)
        if m:
            pid = int(m.group(1))
    except Exception:
        try:
            out = subprocess.check_output(["launchctl", "list"], text=True)
            for line in out.splitlines():
                if AGENT_LABEL in line:
                    parts = line.split()
                    if parts and parts[0].isdigit() and int(parts[0]) > 0:
                        running = True
                        pid = int(parts[0])
                    break
        except Exception:
            pass
    if pid:
        try:
            etime = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "etime="], text=True
            ).strip()
        except Exception:
            pass
    return {"up": running, "pid": pid, "etime": etime, "label": AGENT_LABEL}


def tip_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(BOT_DIR), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None



def parse_regime_log(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": None, "reason": None, "burn_usd": None, "source": "log"}
    for line in reversed(lines or []):
        if "REGIME mode=" not in line:
            continue
        m = re.search(r"REGIME mode=(\w+)\s+reason=(.+?)\s+burn=([0-9.]+)", line)
        if m:
            out["mode"] = m.group(1)
            out["reason"] = m.group(2).strip()
            try:
                out["burn_usd"] = float(m.group(3))
            except ValueError:
                pass
            break
    return out


def kill_line(equity: float, peak: float) -> dict[str, Any]:
    if peak <= START_BANKROLL:
        line = KILL_FLOOR
    else:
        line = max(peak * 0.90, KILL_FLOOR)
    return {
        "kill_line": round(line, 2),
        "dollars_to_kill": round(equity - line, 2),
        "peak": round(peak, 2),
        "equity": round(equity, 2),
        "weekly_stop": WEEKLY_STOP,
        "dollars_to_weekly_stop": round(equity - WEEKLY_STOP, 2),
    }


def soft_hard(reason: str | None) -> str:
    if not reason:
        return "approved"
    r = reason.lower()
    soft = (
        "gemini_cooldown",
        "gemini_cascade",
        "parse",
        "grok_abstain",
        "research_failed",
        "low_confidence",
        "stale_data",
    )
    hard = (
        "spread_too_wide",
        "edge_too_small",
        "mid_outside_band",
        "kill",
        "halt",
        "daily_loss",
    )
    if any(s in r for s in soft) or r.startswith("gemini_error"):
        return "soft_ai"
    if any(h in r for h in hard):
        return "hard_gate"
    return "other"


def normalize_reason(reason: str | None) -> str:
    if not reason:
        return "approved"
    if reason.startswith("gemini_error:gemini_cooldown"):
        return "gemini_cooldown"
    if reason.startswith("gemini_error:gemini_cascade"):
        return "gemini_cascade_exhausted"
    if reason.startswith("gemini_error"):
        return "gemini_error"
    return reason.split(":")[0] if ":" in reason and len(reason) > 40 else reason


def tail_file(path: Path, n: int = 80) -> list[str]:
    if not path.exists():
        return [f"(missing {path})"]
    try:
        out = subprocess.check_output(["tail", f"-n{n}", str(path)], text=True, errors="replace")
        return out.splitlines()
    except Exception as e:
        return [f"(tail error: {e})"]





def _infer_instrument(question: str | None, category: str | None = None) -> str:
    blob = f"{question or ''} {category or ''}".lower()
    rules = (
        ("BTC", ("bitcoin", "btc")),
        ("ETH", ("ethereum", "ether", " eth")),
        ("SOL", ("solana", " sol")),
        ("XAU", ("xauusd", "xau", "gold")),
        ("XAG", ("xagusd", "xag", "silver")),
        ("EURUSD", ("eurusd", "eur/usd", "eur usd")),
        ("GBPUSD", ("gbpusd", "gbp/usd")),
        ("USDJPY", ("usdjpy", "usd/jpy")),
        ("DXY", ("dxy", "dollar index", "us dollar index")),
        ("USDARS", ("usd to ars", "argentina")),
    )
    for label, keys in rules:
        if any(k.strip() in blob for k in keys):
            return label
    cat = (category or "").strip()
    if cat and cat.lower() not in ("other", ""):
        return cat.upper()[:16]
    q = (question or "").strip()
    return (q[:24] + "…") if len(q) > 24 else (q or "—")



def _enrich_event(e) -> dict:
    row = dict(e)
    kind = row.get("kind") or ""
    message = row.get("message") or ""
    payload = row.get("payload_json")
    instrument = question = category = None
    if payload:
        try:
            p = json.loads(payload)
            instrument = p.get("instrument")
            question = p.get("question")
            category = p.get("category")
            if not instrument and not question:
                mk = p.get("market") if isinstance(p.get("market"), dict) else None
                if mk:
                    instrument = mk.get("instrument")
                    question = mk.get("question")
                    category = category or mk.get("category")
        except Exception:
            pass
    if not instrument and question:
        instrument = _infer_instrument(question, category)
    # Primary display: prefer John's stamped message "INSTRUMENT | reason | question"
    if kind == "TRADE_REJECTED" and message and " | " in message:
        display = message.replace(" | ", " · ")
    elif kind == "TRADE_REJECTED" and (instrument or question):
        q = (question or "")[:70]
        display = " · ".join(x for x in (instrument or "—", message or "reject", q) if x)
    else:
        display = message
    return {
        "ts": row.get("ts"),
        "kind": kind,
        "message": message,
        "instrument": instrument,
        "question": question,
        "display": display,
    }



def build_trade_rounds(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per market ticket: Side · Entry · Exit · P&L (open=unrealized, closed=realized)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        tid = str(f.get("token_id") or "") or f"fill-{f.get('fill_id')}"
        by.setdefault(tid, []).append(f)
    rounds: list[dict[str, Any]] = []
    for tid, rows in by.items():
        rows = sorted(
            rows,
            key=lambda r: (str(r.get("ts") or ""), int(r.get("fill_id") or 0)),
        )
        buy_sh = buy_cost = 0.0
        sell_sh = sell_proceeds = 0.0
        entry_ts = exit_ts = None
        instrument = question = category = mode = None
        mark = None
        for r in rows:
            instrument = r.get("instrument") or instrument
            question = r.get("question") or question
            category = r.get("category") or category
            mode = r.get("mode") or mode
            if r.get("mark") is not None:
                mark = r.get("mark")
            sh = float(r.get("shares") or 0)
            px = float(r.get("entry_price") or 0)
            side = (r.get("side") or "BUY").upper()
            if side == "BUY":
                if entry_ts is None:
                    entry_ts = r.get("ts")
                buy_sh += sh
                buy_cost += sh * px
            else:
                exit_ts = r.get("ts")
                sell_sh += sh
                sell_proceeds += sh * px
        entry_px = (buy_cost / buy_sh) if buy_sh > 1e-12 else None
        exit_px = (sell_proceeds / sell_sh) if sell_sh > 1e-12 else None
        net = buy_sh - sell_sh
        matched = min(buy_sh, sell_sh)
        closed = abs(net) <= 1e-6 and matched > 1e-12
        if closed:
            pnl = None
            if entry_px is not None and exit_px is not None:
                pnl = round((exit_px - entry_px) * matched, 4)
            rounds.append(
                {
                    "token_id": tid,
                    "instrument": instrument or "—",
                    "question": (question or "")[:90],
                    "category": category or "",
                    "side": "BUY" if buy_sh >= sell_sh else "SELL",
                    "entry_price": None if entry_px is None else round(entry_px, 4),
                    "exit_price": None if exit_px is None else round(exit_px, 4),
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "shares": round(matched, 4),
                    "size_usd": round(matched * (entry_px or 0), 4),
                    "pnl": pnl,
                    "pnl_kind": "realized",
                    "live": False,
                    "status": "CLOSED",
                    "mode": mode or "PAPER",
                    "fill_ids": [r.get("fill_id") for r in rows],
                }
            )
        else:
            # Open residual — P&L is unrealized on remaining shares
            open_sh = abs(net)
            side = "BUY" if net > 0 else "SELL"
            entry_for_open = entry_px if side == "BUY" else exit_px
            # Prefer live mark from enriched fill
            for r in reversed(rows):
                if r.get("mark") is not None:
                    mark = r.get("mark")
                    break
            unreal = None
            if mark is not None and entry_for_open is not None and open_sh > 0:
                if side == "BUY":
                    unreal = (float(mark) - entry_for_open) * open_sh
                else:
                    unreal = (entry_for_open - float(mark)) * open_sh
            rounds.append(
                {
                    "token_id": tid,
                    "instrument": instrument or "—",
                    "question": (question or "")[:90],
                    "category": category or "",
                    "side": side,
                    "entry_price": None if entry_for_open is None else round(entry_for_open, 4),
                    "exit_price": None,
                    "entry_ts": entry_ts if side == "BUY" else exit_ts,
                    "exit_ts": None,
                    "shares": round(open_sh, 4),
                    "size_usd": round(open_sh * (entry_for_open or 0), 4),
                    "pnl": None if unreal is None else round(unreal, 4),
                    "pnl_kind": "unrealized",
                    "live": True,
                    "status": "OPEN",
                    "mode": mode or "PAPER",
                    "mark": mark,
                    "fill_ids": [r.get("fill_id") for r in rows],
                }
            )
    rounds.sort(key=lambda r: str(r.get("entry_ts") or ""), reverse=True)
    return rounds


def _enrich_fill(r, trading_mode: str = "paper") -> dict:
    row = dict(r)
    question = row.get("m_question") or ""
    category = row.get("m_category") or ""
    instrument = _infer_instrument(question, category)
    entry = float(row["entry_price"] or 0)
    shares = float(row["shares"] or 0)
    mark = row.get("mark_mid")
    if mark is None:
        mark = row.get("mark_yes")
    if mark is None:
        mark = row.get("mark_bid")
    mark_f = float(mark) if mark is not None else None
    side = (row.get("fill_side") or "BUY").upper()
    if mark_f is not None and shares:
        # YES buy: unreal = (mark - entry) * shares
        if side == "BUY":
            unreal = (mark_f - entry) * shares
        else:
            unreal = (entry - mark_f) * shares
    else:
        unreal = None
    pos_shares = float(row["pos_shares"] or 0)
    live = abs(pos_shares) > 1e-9
    notional = entry * shares
    return {
        "fill_id": row.get("fill_id"),
        "ts": row.get("fill_ts"),
        "token_id": row.get("token_id"),
        "instrument": instrument,
        "question": (question or "")[:90],
        "category": category,
        "side": side,
        "entry_price": entry,
        "mark": mark_f,
        "shares": shares,
        "size_usd": round(notional, 4),
        "unrealized": None if unreal is None else round(unreal, 4),
        "decision_id": row.get("decision_id"),
        "order_id": row.get("order_id"),
        "live": live,
        "status": "LIVE" if live else "CLOSED",
        "mode": (trading_mode or "paper").upper(),
        "order_status": row.get("order_status"),
    }

def _enrich_reject(r) -> dict:
    row = dict(r)
    mk = market_from_gates(row.get("gates_json"))
    question = mk.get("question") or row.get("m_question")
    category = mk.get("category") or row.get("m_category")
    market_id = mk.get("market_id") or row.get("market_id")
    instrument = mk.get("instrument") or _infer_instrument(question, category)
    return {
        "ts": row.get("ts"),
        "instrument": instrument or "—",
        "question": (question or "")[:100] or "—",
        "category": category or "",
        "market_id": market_id,
        "reject_reason": row.get("reject_reason"),
        "raw_edge": row.get("raw_edge"),
        "execution_adjusted_edge": row.get("execution_adjusted_edge"),
    }


def parse_reject_log_lines(lines: list[str], n: int = 12) -> list[dict]:
    out = []
    for line in reversed(lines):
        if "REJECT instrument=" not in line:
            continue
        # ... INFO REJECT instrument=BTC reason=grok_abstain Will bitcoin...
        try:
            part = line.split("REJECT instrument=", 1)[1]
            instrument, rest = part.split(" reason=", 1)
            reason, _, question = rest.partition(" ")
            out.append({
                "instrument": instrument.strip(),
                "reject_reason": reason.strip(),
                "question": question.strip()[:100],
                "source": "log",
            })
        except Exception:
            continue
        if len(out) >= n:
            break
    return out

def market_from_gates(gates_json: str | None) -> dict:
    """Pull instrument/question/category/market_id from gates.market (be2e13d+)."""
    out = {"instrument": None, "question": None, "category": None, "market_id": None}
    if not gates_json:
        return out
    try:
        g = json.loads(gates_json)
        m = g.get("market") or {}
        for k in out:
            if m.get(k) is not None:
                out[k] = m.get(k)
        # detail fallback "BTC | Will bitcoin..."
        if not out["instrument"] and m.get("detail"):
            detail = str(m["detail"])
            if " | " in detail:
                inst, q = detail.split(" | ", 1)
                out["instrument"] = inst.strip() or out["instrument"]
                out["question"] = out["question"] or q.strip()
    except Exception:
        pass
    return out


def spread_bucket_from_gates(gates_json: str | None) -> str | None:
    if not gates_json:
        return None
    try:
        g = json.loads(gates_json)
        sw = g.get("spread_width") or {}
        b = sw.get("bucket")
        if b:
            return str(b)
        # fallback: derive from spread_acceptable / spread_too_wide detail if present
        return None
    except Exception:
        return None


def spread_bucket_from_event(payload_json: str | None) -> str | None:
    if not payload_json:
        return None
    try:
        p = json.loads(payload_json)
        b = p.get("spread_bucket")
        return str(b) if b else None
    except Exception:
        return None

def provider_from_gates(gates_json: str | None) -> str | None:
    if not gates_json:
        return None
    try:
        g = json.loads(gates_json)
        detail = (g.get("ai_provider") or {}).get("detail")
        return detail
    except Exception:
        return None


@app.get("/api/snapshot")
def snapshot() -> JSONResponse:
    return JSONResponse(build_snapshot())


def build_snapshot() -> dict[str, Any]:
    """Full ops payload — used by FastAPI and Streamlit."""
    t0 = time.time()
    agent = agent_status()
    tip = tip_commit()
    payload: dict[str, Any] = {
        "ts": utc_now().isoformat(),
        "agent": agent,
        "tip_commit": tip,
        "db_path": str(DB_PATH),
        "logs": {
            "out": tail_file(OUT_LOG, 50),
            "err": tail_file(ERR_LOG, 60),
        },
    }

    if not DB_PATH.exists():
        payload["error"] = f"DB missing: {DB_PATH}"
        return payload

    fills_rows = []
    with connect() as con:
        state = con.execute("SELECT * FROM system_state WHERE id=1").fetchone()
        snap = con.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        hist = con.execute(
            """
            SELECT ts, cash, equity, reserved_cash, exposure, realized_pnl, unrealized_pnl
            FROM portfolio_snapshots
            ORDER BY id DESC LIMIT 240
            """
        ).fetchall()
        since = (utc_now() - timedelta(hours=2)).isoformat()
        decisions = con.execute(
            """
            SELECT ts, approved, reject_reason, raw_edge, execution_adjusted_edge,
                   gates_json, market_id, side, size_usd
            FROM trade_decisions
            WHERE ts >= ?
            ORDER BY id DESC
            """,
            (since,),
        ).fetchall()
        last5 = con.execute(
            """
            SELECT d.ts, d.reject_reason, d.raw_edge, d.execution_adjusted_edge, d.gates_json,
                   d.market_id, m.question AS m_question, m.category AS m_category
            FROM trade_decisions d
            LEFT JOIN markets m ON m.market_id = d.market_id
            WHERE d.approved=0
            ORDER BY d.id DESC LIMIT 8
            """
        ).fetchall()
        last_approved = con.execute(
            """
            SELECT ts, market_id, side, raw_edge, execution_adjusted_edge, size_usd, gates_json
            FROM trade_decisions
            WHERE approved=1
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        fills_n = con.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]

        fills_rows = con.execute(
            """
            SELECT
              f.id AS fill_id,
              f.ts AS fill_ts,
              f.side AS fill_side,
              f.shares,
              f.price AS entry_price,
              f.fee,
              o.id AS order_id,
              o.decision_id,
              o.market_id,
              COALESCE(f.token_id, o.token_id) AS token_id,
              o.status AS order_status,
              m.question AS m_question,
              m.category AS m_category,
              p.shares AS pos_shares,
              p.avg_price AS pos_avg,
              p.realized_pnl AS pos_realized,
              (
                SELECT ms.midpoint FROM market_snapshots ms
                WHERE ms.market_id = o.market_id
                ORDER BY ms.id DESC LIMIT 1
              ) AS mark_mid,
              (
                SELECT ms.yes_price FROM market_snapshots ms
                WHERE ms.market_id = o.market_id
                ORDER BY ms.id DESC LIMIT 1
              ) AS mark_yes,
              (
                SELECT ms.best_bid FROM market_snapshots ms
                WHERE ms.market_id = o.market_id
                ORDER BY ms.id DESC LIMIT 1
              ) AS mark_bid
            FROM fills f
            LEFT JOIN orders o ON o.id = f.order_id
            LEFT JOIN markets m ON m.market_id = o.market_id
            LEFT JOIN positions p ON p.token_id = COALESCE(f.token_id, o.token_id)
            ORDER BY f.id DESC
            LIMIT 50
            """
        ).fetchall()

        open_pos = con.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(ABS(shares*avg_price)),0) AS notional FROM positions WHERE shares != 0"
            if _has_positions_cols(con)
            else "SELECT 0 AS n, 0 AS notional"
        ).fetchone()
        events = con.execute(
            "SELECT ts, kind, message, payload_json FROM system_events ORDER BY id DESC LIMIT 16"
        ).fetchall()
        cat_mix = con.execute(
            """
            SELECT COALESCE(m.category, '(unknown)') AS category, COUNT(*) AS n
            FROM trade_decisions d
            LEFT JOIN markets m ON m.market_id = d.market_id
            WHERE d.ts >= ?
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 12
            """,
            (since,),
        ).fetchall()
        mkt_cats = con.execute(
            """
            SELECT COALESCE(category, '(unknown)') AS category, COUNT(*) AS n
            FROM markets
            GROUP BY 1
            ORDER BY n DESC
            LIMIT 12
            """
        ).fetchall()

        # Peak = HWM of the *current* bankroll regime.
        # Early paper left $1000 snapshots; after reset to $50 those must not set kill.
        cur_eq_row = con.execute(
            "SELECT equity FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cur_eq = float(cur_eq_row["equity"]) if cur_eq_row else START_BANKROLL
        raw_peak = float(
            con.execute("SELECT COALESCE(MAX(equity), 0) AS peak FROM portfolio_snapshots").fetchone()["peak"]
            or 0
        )
        regime_cap = max(cur_eq, START_BANKROLL) * 1.05
        if raw_peak > max(cur_eq, START_BANKROLL) * 1.5:
            peak = float(
                con.execute(
                    "SELECT COALESCE(MAX(equity), ?) AS peak FROM portfolio_snapshots WHERE equity <= ?",
                    (cur_eq, regime_cap),
                ).fetchone()["peak"]
                or cur_eq
            )
        else:
            peak = raw_peak or cur_eq
        peak = max(peak, cur_eq, START_BANKROLL)

        # daily realized from today's snapshots / decisions — use CLI-aligned fields
        day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        day_snap = con.execute(
            "SELECT realized_pnl FROM portfolio_snapshots WHERE ts >= ? ORDER BY id ASC LIMIT 1",
            (day_start,),
        ).fetchone()

    equity = float(snap["equity"]) if snap else START_BANKROLL
    cash = float(snap["cash"]) if snap else START_BANKROLL
    reserved = float(snap["reserved_cash"]) if snap else 0.0
    realized = float(snap["realized_pnl"]) if snap else 0.0
    # Snapshot unrealized often stays 0 while tickets are open — derive mark gap
    snap_unreal = float(snap["unrealized_pnl"]) if snap else 0.0
    # Prefer DB mark if present; else book PnL vs start bankroll (not cash gap / exposure)
    unrealized = snap_unreal if abs(snap_unreal) > 1e-9 else (equity - START_BANKROLL)
    exposure = float(snap["exposure"]) if snap else 0.0
    marks_meta: dict[str, Any] = {"ok": False, "fetched": 0, "age_ms": None, "err": None}

    # Live CLOB marks each poll — portfolio_snapshots / market_snapshots only move on bot cycles.
    try:
        with connect() as mcon:
            live = live_marks_by_token(mcon)
        by_token = live.get("by_token") or {}
        marks_meta = {
            "ok": bool(live.get("ok")),
            "fetched": int(live.get("fetched") or 0),
            "open": int(live.get("open") or 0),
            "age_ms": round((time.time() - float(live.get("t") or time.time())) * 1000),
            "err": live.get("err"),
        }
        if by_token:
            inv = sum(float(p["notional"]) for p in by_token.values())
            pos_unreal = sum(float(p["unrealized"]) for p in by_token.values())
            if live.get("ok"):
                equity = cash + reserved + inv
                unrealized = pos_unreal
                exposure = inv
                open_notional_live = inv
            else:
                open_notional_live = None
        else:
            open_notional_live = None
    except Exception as exc:
        by_token = {}
        open_notional_live = None
        marks_meta["err"] = str(exc)

    # Ring buffer so the chart gets a point every poll, not only on DB snapshot writes.
    _EQUITY_RING.append(
        {
            "ts": utc_now().isoformat(),
            "cash": float(cash),
            "equity": float(equity),
            "exposure": float(exposure),
        }
    )

    kill = kill_line(equity, peak)

    ai_calls = int(state["ai_call_count"]) if state and state["ai_call_count"] is not None else 0
    regime = parse_regime_log(payload.get("logs", {}).get("out", []) or [])
    est_burn = (
        float(regime["burn_usd"])
        if regime.get("burn_usd") is not None
        else round(ai_calls * ESTIMATED_USD_PER_AI_CALL, 4)
    )
    ai_budget = {
        "session_budget_usd": AI_SESSION_BUDGET_USD,
        "estimated_usd_per_call": ESTIMATED_USD_PER_AI_CALL,
        "ai_calls": ai_calls,
        "ai_calls_utc_day": state["ai_calls_utc_day"] if state else None,
        "estimated_burn_usd": est_burn,
        "budget_remaining_usd": round(AI_SESSION_BUDGET_USD - est_burn, 4),
        "regime_mode": regime.get("mode"),
        "regime_reason": regime.get("reason"),
        "fills": fills_n,
        "unrealized_pnl": round(float(unrealized), 4),
        "cost_is_estimated": True,
        "as_of": utc_now().isoformat(),
    }

    daily_pnl = realized - float(day_snap["realized_pnl"]) if day_snap else realized

    mix: Counter[str] = Counter()
    soft_n = hard_n = other_n = approved_n = 0
    grok_n = gemini_n = micro_n = 0
    fee_edges_reject: list[float] = []
    fee_edges_ok: list[float] = []
    funnel = {
        "candidates": len(decisions),
        "estimates_ok": 0,
        "edge_pass": 0,
        "size_pass": 0,
        "fills": fills_n,
    }
    spread_buckets: Counter[str] = Counter()
    for d in decisions:
        reason = d["reject_reason"]
        nr = normalize_reason(reason)
        mix[nr] += 1
        if nr == "spread_too_wide" or (reason or "").startswith("spread"):
            b = spread_bucket_from_gates(d["gates_json"])
            if b:
                spread_buckets[b] += 1
        sh = soft_hard(reason)
        if d["approved"]:
            approved_n += 1
            soft_hard_bucket = "approved"
            if d["execution_adjusted_edge"] is not None:
                fee_edges_ok.append(float(d["execution_adjusted_edge"]))
        else:
            if sh == "soft_ai":
                soft_n += 1
            elif sh == "hard_gate":
                hard_n += 1
            else:
                other_n += 1
            if d["execution_adjusted_edge"] is not None:
                fee_edges_reject.append(float(d["execution_adjusted_edge"]))
        prov = provider_from_gates(d["gates_json"]) or ""
        if prov.startswith("grok"):
            grok_n += 1
        elif "gemini" in prov:
            gemini_n += 1
        elif "micro" in prov.lower():
            micro_n += 1
        # rough funnel. mid_outside_band is pre-AI: the model was not called.
        if nr not in (
            "research_failed",
            "gemini_cooldown",
            "gemini_cascade_exhausted",
            "gemini_error",
            "grok_abstain",
            "mid_outside_band",
        ):
            funnel["estimates_ok"] += 1
        if nr not in (
            "research_failed",
            "gemini_cooldown",
            "gemini_cascade_exhausted",
            "gemini_error",
            "grok_abstain",
            "mid_outside_band",
            "edge_too_small",
            "low_confidence",
        ):
            funnel["edge_pass"] += 1
        if d["approved"]:
            funnel["size_pass"] += 1

    # Prefer decision gates; fill gaps from TRADE_REJECTED payloads (post c5a2e05)
    if sum(spread_buckets.values()) == 0:
        try:
            with connect() as con2:
                rows = con2.execute(
                    """
                    SELECT payload_json FROM system_events
                    WHERE kind='TRADE_REJECTED' AND ts >= ?
                      AND (message='spread_too_wide' OR ifnull(payload_json,'') LIKE '%spread_bucket%')
                    ORDER BY id DESC LIMIT 500
                    """,
                    (since,),
                ).fetchall()
                for r in rows:
                    b = spread_bucket_from_event(r["payload_json"])
                    if b:
                        spread_buckets[b] += 1
        except Exception:
            pass

    # provider / model from latest out log
    provider_model = None
    for line in reversed(payload["logs"]["out"]):
        if "AI_PROVIDER" in line or "gemini:" in line or "grok:" in line:
            provider_model = line.strip()
            break
    # also from latest decision
    latest_prov = None
    if decisions:
        latest_prov = provider_from_gates(decisions[0]["gates_json"])

    paper_started = parse_ts(state["paper_trading_started_at"]) if state else None
    lock_remaining = None
    if paper_started:
        unlock = paper_started + timedelta(days=7)
        lock_remaining = max(0, (unlock - utc_now()).total_seconds())

    # loop age: last decision or event ts
    last_cycle = None
    if decisions:
        last_cycle = decisions[0]["ts"]
    elif events:
        last_cycle = events[0]["ts"]
    loop_age_s = None
    if last_cycle:
        lt = parse_ts(last_cycle)
        if lt:
            loop_age_s = (utc_now() - lt).total_seconds()

    # parse-miss rate from recent err lines
    err_lines = payload["logs"]["err"][-200:]
    attempts = sum(1 for L in err_lines if "gemini attempt" in L)
    parse_miss = sum(1 for L in err_lines if "parse miss" in L)
    cascade_stage = None
    for L in reversed(err_lines):
        if "gemini attempt model=" in L:
            cascade_stage = L.split("model=", 1)[-1].strip()
            break

    # DB hist is sparse (~cycle); append in-memory ring so the chart moves every poll.
    hist_pts = [
        {
            "ts": r["ts"],
            "cash": float(r["cash"]),
            "equity": float(r["equity"]),
            "exposure": float(r["exposure"]),
        }
        for r in reversed(list(hist))
        if float(r["equity"]) <= max(equity, START_BANKROLL) * 1.25
    ]
    last_hist_t = parse_ts(hist_pts[-1]["ts"]) if hist_pts else None
    for p in _EQUITY_RING:
        pt = parse_ts(str(p["ts"]))
        if last_hist_t and pt and pt <= last_hist_t:
            continue
        hist_pts.append(
            {
                "ts": str(p["ts"]),
                "cash": float(p["cash"]),
                "equity": float(p["equity"]),
                "exposure": float(p["exposure"]),
            }
        )

    mode_s = (state["trading_mode"] if state else "paper")
    mtm_ts = utc_now().isoformat()
    executed = [_enrich_fill(r, mode_s) for r in fills_rows]
    # Net from fills (BUY +, SELL −). Positions table can lag after a full exit
    # (e.g. Sun/Dellavedova still showed LIVE) — fills are source of truth for CLOSED.
    net_by_token: dict[str, float] = {}
    for row_t in executed:
        tid0 = str(row_t.get("token_id") or "")
        if not tid0:
            continue
        sh0 = float(row_t.get("shares") or 0)
        side0 = (row_t.get("side") or "BUY").upper()
        net_by_token[tid0] = net_by_token.get(tid0, 0.0) + (sh0 if side0 == "BUY" else -sh0)

    for t in executed:
        tid = str(t.get("token_id") or "")
        net = float(net_by_token.get(tid, 0.0))
        closed = abs(net) <= 1e-6
        side = (t.get("side") or "BUY").upper()
        if closed:
            t["live"] = False
            t["status"] = "CLOSED"
            t["mtm_live"] = False
            t["mtm_ts"] = None
            t["bid"] = None
            t["ask"] = None
            # Closed tickets: no open MTM (realized lives on the book realized_pnl).
            t["unrealized"] = None
            continue
        # Still open — only the side that matches residual risk is LIVE.
        is_live_side = (net > 0 and side == "BUY") or (net < 0 and side == "SELL")
        if not is_live_side:
            t["live"] = False
            t["status"] = "CLOSED"
            t["mtm_live"] = False
            t["unrealized"] = None
            continue
        m = by_token.get(tid) if by_token else None
        if not m or not m.get("live_book"):
            t["live"] = True
            t["status"] = "LIVE"
            t["mtm_live"] = False
            t["mtm_ts"] = None
            t["bid"] = None
            t["ask"] = None
            continue
        mark = float(m["mark"])
        entry = float(t.get("entry_price") or m.get("entry") or 0)
        shares = abs(net)  # residual open size
        t["mark"] = mark
        t["bid"] = m.get("bid")
        t["ask"] = m.get("ask")
        t["size_usd"] = round(shares * entry, 4)
        if side == "BUY":
            t["unrealized"] = round((mark - entry) * shares, 4)
        else:
            t["unrealized"] = round((entry - mark) * shares, 4)
        t["live"] = True
        t["status"] = "LIVE"
        t["mtm_live"] = True
        t["mtm_ts"] = mtm_ts

    # One row per ticket for the dashboard (not one row per fill).
    executed = build_trade_rounds(executed)
    trades_n = len(executed)
    open_trades_n = sum(1 for r in executed if r.get("live"))
    closed_trades_n = trades_n - open_trades_n

    payload.update(
        {
            "mode": (state["trading_mode"] if state else "unknown").upper()
            if state and not state["halted"]
            else ("HALTED" if state and state["halted"] else "UNKNOWN"),
            "halted": bool(state["halted"]) if state else None,
            "halt_reason": state["halt_reason"] if state else None,
            "paper_started_at": state["paper_trading_started_at"] if state else None,
            "paper_lock_remaining_s": lock_remaining,
            "cash": cash,
            "equity": equity,
            "reserved_cash": reserved,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "start_bankroll": START_BANKROLL,
            "weekly_stop": WEEKLY_STOP,
            "ai_budget": ai_budget,
            "equity_history": hist_pts,
            "exposure": exposure,
            "marks_live": marks_meta,
            "kill": kill,
            "daily_loss_used": round(max(0.0, -daily_pnl), 4),
            "daily_loss_budget": DAILY_LOSS_BUDGET,
            "fills": fills_n,
            "trades": trades_n,
            "open_trades": open_trades_n,
            "closed_trades": closed_trades_n,
            "first_fill": trades_n > 0,
            "executed_trades": executed,
            "open_positions": int(open_pos["n"] or 0),
            "open_notional": float(
                open_notional_live
                if open_notional_live is not None
                else (open_pos["notional"] or 0)
            ),
            "reject_mix": dict(mix.most_common(12)),
            "spread_buckets": {
                k: int(spread_buckets.get(k, 0))
                for k in ("le_6c", "7_10c", "11_15c", "gt_15c")
            },
            "spread_bucket_total": int(sum(spread_buckets.values())),
            "ai_soft_rejects": soft_n,
            "ai_hard_rejects": hard_n,
            "other_rejects": other_n,
            "approved_2h": approved_n,
            "decisions_2h": len(decisions),
            "provider_split": {"grok": grok_n, "gemini": gemini_n, "micro": micro_n},
            "provider_model_log": provider_model,
            "provider_latest_decision": latest_prov,
            "cascade_stage": cascade_stage,
            "parse_miss_rate": round(parse_miss / attempts, 3) if attempts else None,
            "gemini_attempts_window": attempts,
            "funnel": funnel,
            "avg_fee_edge_rejects": round(sum(fee_edges_reject) / len(fee_edges_reject), 5)
            if fee_edges_reject
            else None,
            "avg_fee_edge_approvals": round(sum(fee_edges_ok) / len(fee_edges_ok), 5)
            if fee_edges_ok
            else None,
            "last_approved": dict(last_approved) if last_approved else None,
            "last_5_rejects": [_enrich_reject(r) for r in last5],
            "recent_rejects": (
                parse_reject_log_lines(payload.get("logs", {}).get("out", []))
                or parse_reject_log_lines(payload.get("logs", {}).get("err", []))
            ),
            "events": [_enrich_event(e) for e in events],
            "decision_categories_2h": {r["category"]: r["n"] for r in cat_mix},
            "market_categories": {r["category"]: r["n"] for r in mkt_cats},
            "loop_age_s": loop_age_s,
            "last_cycle_ts": last_cycle,
            "query_ms": round((time.time() - t0) * 1000, 1),
        }
    )
    return payload


def _has_positions_cols(con: sqlite3.Connection) -> bool:
    cols = {r[1] for r in con.execute("PRAGMA table_info(positions)").fetchall()}
    return "shares" in cols and ("avg_price" in cols or "entry_price" in cols)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        content=HTML,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>POLYGROK Ops Console</title>
<style>
  :root {
    --bg:#0b0f14; --panel:#121821; --border:#1e2a38; --text:#e6edf3;
    --muted:#8b9bb0; --ok:#3dd68c; --warn:#f5a524; --bad:#f07178;
    --accent:#59c2ff; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 system-ui,-apple-system,sans-serif}
  header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);position:sticky;top:0;background:rgba(11,15,20,.92);backdrop-filter:blur(8px);z-index:10}
  h1{margin:0;font-size:15px;letter-spacing:.04em}
  .meta{color:var(--muted);font-family:var(--mono);font-size:11px}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:12px 16px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 12px;min-height:74px}
  .card h2{margin:0 0 6px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
  .big{font-size:20px;font-weight:700;font-family:var(--mono)}
  .row{display:flex;gap:8px;flex-wrap:wrap}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:999px;border:1px solid var(--border);font-family:var(--mono);font-size:11px}
  .ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.accent{color:var(--accent)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--muted);display:inline-block}
  .dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .dot.off{background:var(--bad)}
  .wide{grid-column:span 2}
  .full{grid-column:1/-1}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px}
  th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:600}
  .console{background:#070b10;border:1px solid var(--border);border-radius:8px;padding:8px;height:220px;overflow:auto;font-family:var(--mono);font-size:11px;white-space:pre-wrap;color:#b7c5d3}
  .console .err{color:#f0a0a6}
  .barwrap{display:flex;flex-direction:column;gap:4px}
  .bar{display:flex;align-items:center;gap:8px}
  .bar label{width:150px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar .track{flex:1;height:8px;background:#0b121a;border-radius:4px;overflow:hidden}
  .bar .fill{height:100%;background:linear-gradient(90deg,#2a7,#59c2ff)}
  .bar .n{width:36px;text-align:right;font-family:var(--mono)}
  .funnel{display:flex;gap:6px;align-items:stretch}
  .funnel div{flex:1;background:#0b121a;border-radius:8px;padding:8px;text-align:center;border:1px solid var(--border)}
  .funnel strong{display:block;font-size:16px;font-family:var(--mono)}
  .funnel span{color:var(--muted);font-size:10px;text-transform:uppercase}
  .chart-wrap{position:relative;height:180px;background:#070b10;border:1px solid var(--border);border-radius:8px;padding:8px 8px 4px}
  .chart-wrap canvas{width:100%;height:100%;display:block}
  @media (max-width:1100px){.grid{grid-template-columns:repeat(2,1fr)}.wide,.full{grid-column:span 2}}
</style>
</head>
<body>
<header>
  <div>
    <h1>POLYGROK · Ops Console</h1>
    <div class="meta" id="sub">loading…</div>
  </div>
  <div class="row" id="headerPills"></div>
</header>
<main class="grid" id="main"></main>
<script>
const $ = (id)=>document.getElementById(id);
function fmtDur(s){
  if(s==null) return '—';
  s=Math.floor(s);
  const d=Math.floor(s/86400), h=Math.floor((s%86400)/3600), m=Math.floor((s%3600)/60);
  if(d) return `${d}d ${h}h`;
  if(h) return `${h}h ${m}m`;
  return `${m}m ${s%60}s`;
}
/** Date + time in Asia/Calcutta for every KPI / table row. */
function fmtTs(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  if(Number.isNaN(d.getTime())) return String(iso);
  try{
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Calcutta',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false
    }).format(d).replace(',', '') + ' IST';
  }catch(e){
    return d.toISOString();
  }
}
function asOf(d){ return fmtTs(d.ts); }

function clsMode(m){
  if(m==='PAPER') return 'ok';
  if(m==='HALTED') return 'bad';
  return 'warn';
}
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;' }[c]));}

function drawEquityChart(d){
  const canvas=document.getElementById('equityChart');
  if(!canvas) return;
  const ctx=canvas.getContext('2d');
  const W=canvas.width, H=canvas.height;
  ctx.clearRect(0,0,W,H);
  const hist=d.equity_history||[];
  const start=Number(d.start_bankroll||5000);
  let pts=hist.map(p=>({t:Date.parse(p.ts), eq:Number(p.equity), cash:Number(p.cash), exp:Number(p.exposure||0)}));
  // always include latest point
  pts.push({t:Date.now(), eq:Number(d.equity), cash:Number(d.cash), exp:Number(d.exposure||0)});
  if(pts.length<2){
    // flat line at current
    pts=[{t:Date.now()-3600000, eq:start, cash:start, exp:0}, pts[pts.length-1]];
  }
  const eqs=pts.map(p=>p.eq), cashes=pts.map(p=>p.cash);
  let ymin=Math.min(start, ...eqs, ...cashes);
  let ymax=Math.max(start, ...eqs, ...cashes);
  const pad=Math.max(0.5, (ymax-ymin)*0.15);
  ymin-=pad; ymax+=pad;
  const t0=pts[0].t, t1=pts[pts.length-1].t || t0+1;
  const x=t=> 40 + (W-50)*((t-t0)/Math.max(1,t1-t0));
  const y=v=> H-22 - (H-34)*((v-ymin)/Math.max(1e-9, ymax-ymin));
  // grid
  ctx.strokeStyle='#1e2a38'; ctx.lineWidth=1;
  for(let i=0;i<4;i++){
    const yy=20+(H-40)*i/3;
    ctx.beginPath(); ctx.moveTo(40,yy); ctx.lineTo(W-8,yy); ctx.stroke();
  }
  // start bankroll dotted
  ctx.setLineDash([4,4]);
  ctx.strokeStyle='#f5a524'; ctx.lineWidth=1.5;
  ctx.beginPath(); ctx.moveTo(40,y(start)); ctx.lineTo(W-8,y(start)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle='#f5a524'; ctx.font='11px ui-monospace,monospace';
  ctx.fillText('start $'+start.toFixed(0), 44, Math.max(12, y(start)-4));
  // cash thin
  ctx.strokeStyle='#8b9bb0'; ctx.lineWidth=1;
  ctx.beginPath();
  pts.forEach((p,i)=>{ const X=x(p.t), Y=y(p.cash); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y); });
  ctx.stroke();
  // equity solid accent
  ctx.strokeStyle='#59c2ff'; ctx.lineWidth=2.25;
  ctx.beginPath();
  pts.forEach((p,i)=>{ const X=x(p.t), Y=y(p.eq); i?ctx.lineTo(X,Y):ctx.moveTo(X,Y); });
  ctx.stroke();
  // end dots
  const last=pts[pts.length-1];
  ctx.fillStyle='#59c2ff'; ctx.beginPath(); ctx.arc(x(last.t), y(last.eq), 3.5, 0, Math.PI*2); ctx.fill();
  ctx.fillStyle='#e6edf3'; ctx.font='12px ui-monospace,monospace';
  ctx.fillText('$'+last.eq.toFixed(2), Math.min(W-70, x(last.t)+8), y(last.eq)-6);
  // y labels
  ctx.fillStyle='#8b9bb0'; ctx.font='10px ui-monospace,monospace';
  ctx.fillText(ymax.toFixed(1), 4, 18);
  ctx.fillText(ymin.toFixed(1), 4, H-10);
}

async function tick(){
  try{
    const r = await fetch('/api/snapshot?'+Date.now());
    if(!r.ok){ $('sub').textContent = 'poll HTTP '+r.status; return; }
    const d = await r.json();
    if(d.error){ $('sub').textContent = 'DB: '+d.error; }
    render(d);
  }catch(e){
    $('sub').textContent = 'poll failed: '+e;
  }
}
function render(d){
  const up = d.agent?.up;
  $('sub').textContent = `as of ${fmtTs(d.ts)} · query ${d.query_ms}ms · tip ${d.tip_commit||'?'} · loop age ${fmtDur(d.loop_age_s)}`;
  $('headerPills').innerHTML = `
    <span class="pill"><span class="dot ${up?'on':'off'}"></span>LaunchAgent ${up?'UP':'DOWN'}${d.agent?.pid? ' · pid '+d.agent.pid:''}${d.agent?.etime? ' · '+d.agent.etime:''}</span>
    <span class="pill ${clsMode(d.mode)}">${esc(d.mode)}${d.halted? ' · '+esc(d.halt_reason||'halted'):''}</span>
    <span class="pill accent">${esc(d.provider_latest_decision||d.provider_model_log||'provider?')}</span>
    <span class="pill">${d.first_fill?'HAS TRADES ✓':'no trades yet'}</span>
  `;
  function bars(obj){
    const entries=Object.entries(obj||{});
    if(!entries.length) return '<div class="meta">—</div>';
    const max=Math.max(1,...entries.map(([,v])=>v));
    return entries.map(([k,v])=>`<div class="bar"><label title="${esc(k)}">${esc(k)}</label><div class="track"><div class="fill" style="width:${(100*v/max).toFixed(1)}%"></div></div><div class="n">${v}</div></div>`).join('');
  }
  const mix = d.reject_mix||{};
  const maxN = Math.max(1,...Object.values(mix));
  const mixHtml = Object.entries(mix).map(([k,v])=>`
    <div class="bar"><label title="${esc(k)}">${esc(k)}</label><div class="track"><div class="fill" style="width:${(100*v/maxN).toFixed(1)}%"></div></div><div class="n">${v}</div></div>`).join('')||'<div class="meta">no decisions in window</div>';
  const mktCatHtml = bars(d.market_categories);
  const spreadHtml = bars(d.spread_buckets);
  const decCatHtml = bars(d.decision_categories_2h);
  const rejRows = (d.last_5_rejects||[]).map(r=>`<tr><td>${fmtTs(r.ts)}</td><td class="accent">${esc(r.instrument||'—')}</td><td title="${esc(r.question||'')}">${esc((r.question||'—').slice(0,64))}</td><td>${esc((r.reject_reason||'').slice(0,36))}</td><td>${r.raw_edge??'—'}</td></tr>`).join('');
  const mtmClock = fmtTs(d.ts);
  const tradeRows = (d.executed_trades||[]).map(t=>{
    const pnl = t.pnl==null ? '—' : ((t.pnl>=0?'+':'')+Number(t.pnl).toFixed(2));
    const pnlCls = t.pnl==null ? '' : (t.pnl>=0?'ok':'bad');
    const pnlLabel = t.pnl_kind==='realized' ? 'closed' : (t.live ? 'open' : '');
    const capital = t.size_usd!=null ? ('$'+Number(t.size_usd).toFixed(2)) : '—';
    return `<tr>
        <td class="accent">${esc(t.instrument||'—')}</td>
        <td title="${esc(t.question||'')}">${esc((t.question||'—').slice(0,56))}</td>
        <td>${esc(t.side||'')}</td>
        <td>${t.entry_price!=null?Number(t.entry_price).toFixed(2):'—'}</td>
        <td>${t.exit_price!=null?Number(t.exit_price).toFixed(2):'—'}</td>
        <td>${capital}</td>
        <td class="mtm-unreal ${pnlCls}">${pnl}${pnlLabel?`<div class="meta">${pnlLabel}</div>`:''}</td>
        <td><span class="pill ${t.live?'ok':'warn'}">${esc(t.status||'')}</span></td>
        <td class="meta">${fmtTs(t.entry_ts)}</td>
        <td class="meta">${t.exit_ts?fmtTs(t.exit_ts):'—'}</td>
      </tr>`;
  }).join('');
  const liveRej = (d.recent_rejects||[]).map(r=>`<tr><td>${fmtTs(r.ts||d.ts)}</td><td class="accent">${esc(r.instrument||'—')}</td><td title="${esc(r.question||'')}">${esc((r.question||'—').slice(0,72))}</td><td>${esc((r.reject_reason||'').slice(0,40))}</td></tr>`).join('');
  const evRows = (d.events||[]).map(e=>`<tr><td>${fmtTs(e.ts)}</td><td>${esc(e.kind)}</td><td title="${esc(e.question||e.message||'')}">${esc((e.display||e.message||'').slice(0,90))}</td></tr>`).join('');
  const f = d.funnel||{};
  const outLog = (d.logs?.out||[]).slice(-40).map(esc).join('\\n');
  const errLog = (d.logs?.err||[]).slice(-50).map(l=>`<span class="err">${esc(l)}</span>`).join('\\n');
  $('main').innerHTML = `
    <div class="card"><h2>Portfolio (Equity)</h2><div class="big">$${Number(d.equity).toFixed(2)}</div>
      <div class="meta">cash $${Number(d.cash).toFixed(2)} + open marks · <b>not</b> profit</div>
      <div class="meta">closed PnL (realized) $${Number(d.realized_pnl).toFixed(2)} · open MTM (unrealized) $${Number(d.unrealized_pnl).toFixed(2)} · peak $${Number(d.kill?.peak||0).toFixed(2)}</div>
      <div class="meta">as of ${asOf(d)}</div></div>
    <div class="card"><h2>$ to kill</h2><div class="big ${d.kill?.dollars_to_kill>250?'ok':(d.kill?.dollars_to_kill>100?'warn':'bad')}">$${Number(d.kill?.dollars_to_kill).toFixed(2)}</div>
      <div class="meta">line $${Number(d.kill?.kill_line).toFixed(2)} · weekly $${Number(d.kill?.weekly_stop||d.weekly_stop||0).toFixed(0)} · daily $${Number(d.daily_loss_used).toFixed(2)} / $${d.daily_loss_budget}</div>
      <div class="meta">as of ${asOf(d)}</div></div>
    <div class="card"><h2>AI budget</h2><div class="big ${(d.ai_budget?.regime_mode==='ATTACK')?'ok':(d.ai_budget?.regime_mode==='DEFEND'?'warn':'bad')}">${esc(d.ai_budget?.regime_mode||'—')} · $${Number(d.ai_budget?.estimated_burn_usd||0).toFixed(2)} / $${Number(d.ai_budget?.session_budget_usd||10).toFixed(0)}</div>
      <div class="meta">est. · ${d.ai_budget?.ai_calls||0} calls · left $${Number(d.ai_budget?.budget_remaining_usd||0).toFixed(2)} · ${esc(d.ai_budget?.regime_reason||'')}</div>
      <div class="meta">as of ${fmtTs(d.ai_budget?.as_of||d.ts)}</div></div>
    <div class="card"><h2>Trades + PnL</h2><div class="big">${d.trades??d.executed_trades?.length??0} trades</div>
      <div class="meta">open ${d.open_trades??0} · closed ${d.closed_trades??0} · notional $${Number(d.open_notional).toFixed(2)} · approved 2h ${d.approved_2h}</div>
      <div class="meta">as of ${asOf(d)}</div></div>
    <div class="card"><h2>Paper lock</h2><div class="big">${fmtDur(d.paper_lock_remaining_s)}</div>
      <div class="meta">started ${fmtTs(d.paper_started_at)} · as of ${asOf(d)}</div></div>

    <div class="card full"><h2>Equity live · dotted line = start bankroll $${Number(d.start_bankroll||5000).toFixed(0)}</h2>
      <div class="meta">as of ${asOf(d)}</div>
      <div class="chart-wrap"><canvas id="equityChart" width="1100" height="180"></canvas></div>
      <div class="meta" style="margin-top:6px">blue = equity · grey = cash · dotted amber = start bankroll · unreal ${Number(d.unrealized_pnl||0).toFixed(4)} · in market ${Number(d.exposure||0).toFixed(2)} · marks ${d.marks_live?.ok?('LIVE '+ (d.marks_live.fetched||0)+'/'+(d.marks_live.open||0)):'DB'}</div>
    </div>
    <div class="card wide"><h2>Edge funnel (2h)</h2><div class="meta">as of ${asOf(d)}</div>
      <div class="funnel">
        <div><strong>${f.candidates||0}</strong><span>candidates</span></div>
        <div><strong>${f.estimates_ok||0}</strong><span>est OK</span></div>
        <div><strong>${f.edge_pass||0}</strong><span>edge pass</span></div>
        <div><strong>${f.size_pass||0}</strong><span>size pass</span></div>
        <div><strong>${d.trades??f.fills||0}</strong><span>trades</span></div>
      </div>
      <div class="meta" style="margin-top:8px">avg fee-edge rejects ${d.avg_fee_edge_rejects??'—'} · approvals ${d.avg_fee_edge_approvals??'—'}</div>
    </div>
    <div class="card wide"><h2>Provider health</h2><div class="meta">as of ${asOf(d)}</div>
      <div class="row" style="margin-bottom:6px">
        <span class="pill">Grok ${d.provider_split?.grok||0}</span>
        <span class="pill">Gemini ${d.provider_split?.gemini||0}</span>
        <span class="pill">Micro ${d.provider_split?.micro||0}</span>
        <span class="pill">cascade ${esc(d.cascade_stage||'—')}</span>
        <span class="pill">parse-miss ${d.parse_miss_rate==null?'—':(100*d.parse_miss_rate).toFixed(0)+'%'} (${d.gemini_attempts_window||0} att)</span>
      </div>
      <div class="meta">soft AI ${d.ai_soft_rejects} · hard gates ${d.ai_hard_rejects} · other ${d.other_rejects} · decisions 2h ${d.decisions_2h}</div>
      <div class="meta">${esc(d.provider_model_log||'')}</div>
    </div>

    <div class="card"><h2>Markets by category</h2><div class="meta">as of ${asOf(d)}</div><div class="barwrap">${mktCatHtml}</div></div>
    <div class="card"><h2>Decisions 2h by category</h2><div class="meta">as of ${asOf(d)}</div><div class="barwrap">${decCatHtml}</div></div>
    <div class="card"><h2>Rejected spread width (2h)</h2><div class="meta">as of ${asOf(d)}</div><div class="barwrap">${spreadHtml}</div>
      <div class="meta">buckets from gates.spread_width / TRADE_REJECTED · n=${d.spread_bucket_total||0}</div></div>
    <div class="card wide"><h2>Reject mix (2h)</h2><div class="meta">as of ${asOf(d)}</div><div class="barwrap">${mixHtml}</div></div>
    <div class="card full"><h2>Trades · as of ${mtmClock}</h2>
      <div class="meta">One row per ticket — open P&amp;L while live, closed P&amp;L when exited</div>
      <table><thead><tr>
        <th>market</th><th>question</th><th>side</th>
        <th>entry</th><th>exit</th><th>traded capital</th><th>P&amp;L ($)</th><th>status</th>
        <th>opened</th><th>closed</th>
      </tr></thead>
      <tbody>${tradeRows||'<tr><td colspan=10 class="meta">no trades yet</td></tr>'}</tbody></table>
    </div>
    <div class="card wide"><h2>Last rejects — instrument · question · reason</h2><div class="meta">as of ${asOf(d)}</div>
      <table><thead><tr><th>date · time</th><th>inst</th><th>question</th><th>reason</th><th>raw</th></tr></thead><tbody>${rejRows||'<tr><td colspan=5>—</td></tr>'}</tbody></table>
    </div>
    <div class="card wide"><h2>Live REJECT log</h2><div class="meta">as of ${asOf(d)}</div>
      <table><thead><tr><th>date · time</th><th>inst</th><th>question</th><th>reason</th></tr></thead><tbody>${liveRej||'<tr><td colspan=3 class="meta">waiting for REJECT instrument=… lines after bounce</td></tr>'}</tbody></table>
    </div>

    <div class="card wide"><h2>stdout · launchd.out.log</h2><div class="console">${outLog}</div></div>
    <div class="card wide"><h2>stderr · launchd.err.log</h2><div class="console">${errLog}</div></div>

    <div class="card full"><h2>system_events</h2><div class="meta">as of ${asOf(d)}</div>
      <table><thead><tr><th>date · time</th><th>kind</th><th>instrument · reason · question</th></tr></thead><tbody>${evRows}</tbody></table>
    </div>
  `;
  drawEquityChart(d);
}
tick();
setInterval(tick, 4000);
</script>
</body>
</html>
"""

