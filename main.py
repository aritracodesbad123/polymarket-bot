#!/usr/bin/env python3
"""LEGACY — frozen. Do not extend.

POLYGROK lives in `app/` (`python -m app.cli`). This file is the old Gemini
paper bot (CLOB V1 / py-clob-client). Its paper clock does not count toward
POLYGROK's 7-day live lock.

Polymarket paper bot — article stack, Pay-or-Die constraints.

Pipeline (Vincent, Jun 2026):
    DATA → AI/heuristic p → MATH (EV, Quarter Kelly, Bayes, log-return) → GTC paper fills → SQLite + JSON log

live_execution = False. This process never signs a CLOB order.

Env (optional):
    GEMINI_API_KEY / GOOGLE_API_KEY   preferred brain
    GEMINI_MODEL                      default gemini-3.6-flash
    ANTHROPIC_API_KEY                 Claude fallback if Gemini is unset
    AI_SCAN_LIMIT                     max AI calls per cycle (default 2)

Run:
    python main.py --check
    python main.py --probe
    python main.py --once
    python main.py --hours 6 --fresh    # paper session, then exit
    python main.py --status             # print current paper_trade_log.txt
    python main.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DIR = Path(__file__).resolve().parent
LOG_JSON = DIR / "paper_trade_log.txt"
LOG_FILE = DIR / "bot.log"
DB_PATH = DIR / "positions.db"
PID_PATH = DIR / "paper_bot.pid"


def _load_env() -> None:
    """Read .env next to this file without overriding a real shell export."""
    path = DIR / ".env"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()

INITIAL_BANKROLL = 50.00
KILL_BALANCE = 45.00
DAILY_QUOTA_USD = 0.67
GRACE_SECONDS = 48 * 60 * 60
QUOTA_WINDOW_SECONDS = 24 * 60 * 60
MIN_ORDER_USD = 1.00
EPSILON = 0.20
NETWORK_COOLDOWN_SECONDS = 30
LOOP_SECONDS = 12
MARKET_SCAN_LIMIT = 50
BOOK_WORKERS = 8
EV_EDGE_MIN = 0.05  # skip unless |p - price| >= 5%
KELLY_FRACTION = 0.25  # article: Quarter Kelly, not uncapped
MAX_SLIPPAGE = 0.03  # directional take: skip if spread/mid wider than this
WIDE_SPREAD = 0.03
TIGHT_SPREAD = 0.02
PRICE_LO, PRICE_HI = 0.22, 0.78  # skip lottery tickets
MIN_VOLUME_24H = 2_000.0
MIN_TOP_SIZE = 40.0  # shares at best bid/ask
MAX_POSITIONS = 2
CLIP_USD = 1.50
MIN_HOLD_SECONDS = 120
TAKE_PROFIT = 0.02
STOP_LOSS = 0.03
TREND_BARS = 8
TREND_MOVE = 0.025
REVERSION_SPIKE = 0.04
AI_SCAN_LIMIT = int(os.environ.get("AI_SCAN_LIMIT") or os.environ.get("CLAUDE_SCAN_LIMIT", "2"))
AI_COOLDOWN_SECONDS = 90  # ponytail: fixed 90s on 429; Retry-After if quota stays dead

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
VERTEX_GEMINI = "https://aiplatform.googleapis.com/v1/publishers/google/models"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
ANTHROPIC_HOST = "https://api.anthropic.com/v1/messages"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
# generateContent text models from https://ai.google.dev/gemini-api/docs/models — skip image/live/tts/media
GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)

ARB_EDGE = 0.012  # YES ask + NO ask <= 0.988 → locked $1 payout
BANDIT_ARMS = ("SPREAD_SCALPER", "TREND_RIDER", "MEAN_REVERSION")
STRATEGIES = ("COMPLEMENT_ARB",) + BANDIT_ARMS

PROMPT = """Estimate the probability this Polymarket market resolves YES.
Use the base rate: most headlines are less extreme than they read.
Penalize extreme confidence. Do not echo the market mid as your answer.
Question: {question}
YES mid: {mid:.3f}
Spread: {spread:.3f}
Reply with one JSON object only, no markdown, no extra text:
{{"probability": 0.xx, "confidence": "high"|"medium"|"low", "reasoning": "..."}}"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3),
    ],
)
log = logging.getLogger("pay-or-die")


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    retry: bool = True,
) -> Any:
    hdrs = {"User-Agent": "pay-or-die-paper/2.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    while True:
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                raise
            if not retry:
                raise
            log.warning("network/API fault (%s) — cooldown %ss", exc, NETWORK_COOLDOWN_SECONDS)
            time.sleep(NETWORK_COOLDOWN_SECONDS)


def _best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bid = float(bids[-1]["price"]) if bids else None
    ask = float(asks[-1]["price"]) if asks else None
    return bid, ask


def _top_size(levels: list | None) -> float:
    if not levels:
        return 0.0
    try:
        return float(levels[-1]["size"])
    except (KeyError, TypeError, ValueError, IndexError):
        return 0.0


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return last


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def expected_value(p: float, price: float) -> float:
    """EV of buying a $1 YES at `price`. Equals p - price."""
    return p - price


def quarter_kelly(p: float, contract_price: float) -> float:
    """f = 0.25 * (p - c) / (1 - c). No edge → 0."""
    c = _clamp(contract_price, 1e-6, 1 - 1e-6)
    p = _clamp(p, 0.0, 1.0)
    if p <= c:
        return 0.0
    return KELLY_FRACTION * (p - c) / (1.0 - c)


def bayes_update(prior: float, evidence: float, strength: float = 0.30) -> float:
    """Mix prior p with new evidence (mid / last). Strength 0.3 ≈ modest news."""
    return _clamp(prior * (1.0 - strength) + evidence * strength, 0.01, 0.99)


def log_return(p1: float, p0: float) -> float:
    if p0 <= 0 or p1 <= 0:
        return 0.0
    return math.log(p1 / p0)


def _parse_json_object(text: str) -> dict | None:
    text = text.strip()
    fence = text.find("```")
    if fence >= 0:
        chunk = text[fence + 3 :]
        if chunk.lstrip().lower().startswith("json"):
            chunk = chunk.lstrip()[4:]
        end_fence = chunk.find("```")
        if end_fence >= 0:
            chunk = chunk[:end_fence]
        text = chunk
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _gemini_text(data: dict) -> str:
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    return "".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    )


def _gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _gemini_chain() -> list[tuple[str, str, str]]:
    """(model, url, host) — env model first, then docs list; Vertex then AI Studio."""
    models: list[str] = []
    for m in (GEMINI_MODEL, *GEMINI_MODELS):
        if m not in models:
            models.append(m)
    out: list[tuple[str, str, str]] = []
    for model in models:
        out.append((model, f"{VERTEX_GEMINI}/{model}:generateContent", "vertex"))
        out.append((model, f"{GEMINI_API}/{model}:generateContent", "aistudio"))
    return out


def brain_name() -> str:
    if _gemini_key():
        return f"gemini:{GEMINI_MODEL}"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"claude:{CLAUDE_MODEL}"
    return "heuristic"


@dataclass
class Arm:
    attempts: int = 0
    wins: int = 0
    net_profit: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.attempts if self.attempts else 0.0


@dataclass
class Position:
    token_id: str
    question: str
    shares: float
    avg_price: float
    last_mid: float
    strategy: str
    prior_p: float
    opened_at: float = 0.0


@dataclass
class RestingQuote:
    token_id: str
    question: str
    side: str
    price: float
    usd: float
    strategy: str
    created_at: float


class Store:
    """Local position ledger so we don't re-query a chain we aren't on."""

    def __init__(self, path: Path) -> None:
        self.cx = sqlite3.connect(path, check_same_thread=False)
        self.cx.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                token_id TEXT PRIMARY KEY, question TEXT, shares REAL,
                avg_price REAL, last_mid REAL, strategy TEXT, prior_p REAL, opened_at REAL)"""
        )
        self.cx.execute(
            """CREATE TABLE IF NOT EXISTS fills (
                ts REAL, token_id TEXT, side TEXT, shares REAL, price REAL,
                pnl REAL, log_return REAL, strategy TEXT)"""
        )
        self.cx.commit()

    def upsert(self, p: Position) -> None:
        self.cx.execute(
            """INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(token_id) DO UPDATE SET
                 shares=excluded.shares, avg_price=excluded.avg_price,
                 last_mid=excluded.last_mid, strategy=excluded.strategy, prior_p=excluded.prior_p""",
            (p.token_id, p.question, p.shares, p.avg_price, p.last_mid, p.strategy, p.prior_p, p.opened_at or time.time()),
        )
        self.cx.commit()

    def delete(self, token_id: str) -> None:
        self.cx.execute("DELETE FROM positions WHERE token_id=?", (token_id,))
        self.cx.commit()

    def fill(self, **row: Any) -> None:
        self.cx.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), row["token_id"], row["side"], row["shares"], row["price"], row["pnl"], row["log_return"], row["strategy"]),
        )
        self.cx.commit()

    def stats(self) -> tuple[int, float]:
        n, pnl = self.cx.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM fills").fetchone()
        return int(n), float(pnl)

    def load(self) -> dict[str, Position]:
        out = {}
        for r in self.cx.execute(
            "SELECT token_id,question,shares,avg_price,last_mid,strategy,prior_p,opened_at FROM positions"
        ):
            out[r[0]] = Position(*r)
        return out


class PayOrDieBot:
    def __init__(self) -> None:
        self.live_execution = False  # paper trial; live CLOB signing is off
        self.cash = INITIAL_BANKROLL
        self.store = Store(DB_PATH)
        self.positions = self.store.load()
        self.quotes: list[RestingQuote] = []
        self.arms = {s: Arm() for s in STRATEGIES}
        self.mid_history: dict[str, list[float]] = {}
        self.priors: dict[str, float] = {tid: p.prior_p for tid, p in self.positions.items()}
        self.started_at = time.time()
        self.last_quota_check = self.started_at
        self.equity_marks: list[tuple[float, float]] = [(self.started_at, INITIAL_BANKROLL)]
        self.loops = 0
        self.session_started = time.time()
        self.stop_at: float | None = None
        self._arb_taken: dict[str, float] = {}
        self._ai_cool_until = 0.0
        self._dead_books: set[str] = set()  # ponytail: session skip; TTL if a 404 ever comes back live
        self._clob = None
        self._load_log()
        if self.live_execution:
            raise RuntimeError("live_execution must stay False in this paper-trading build")

    def reset_paper(self) -> None:
        """Clean $50 book for a timed paper session. Fill history is kept."""
        self.quotes.clear()
        for tid in list(self.positions):
            self.store.delete(tid)
            del self.positions[tid]
        self.cash = INITIAL_BANKROLL
        self.priors.clear()
        self.mid_history.clear()
        self.arms = {s: Arm() for s in STRATEGIES}
        now = time.time()
        self.started_at = now
        self.last_quota_check = now
        self.session_started = now
        self.loops = 0
        self.equity_marks = [(now, INITIAL_BANKROLL)]
        self._arb_taken = {}
        log.info("fresh paper book $%.2f", self.cash)

    def _clob_client(self):
        if self._clob is not False and self._clob is None:
            try:
                from py_clob_client.client import ClobClient  # type: ignore

                self._clob = ClobClient(CLOB_HOST)
            except Exception:
                self._clob = False
        return self._clob or None

    # ----- persistence -----------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        now = time.time()
        grace = max(0.0, GRACE_SECONDS - (now - self.started_at))
        fills, realized = self.store.stats()
        elapsed = now - self.session_started
        remaining = max(0.0, (self.stop_at - now)) if self.stop_at else None
        return {
            "total_balance": round(self.equity(), 4),
            "cash": round(self.cash, 4),
            "inventory_mark": round(self.equity() - self.cash, 4),
            "target_progress": {
                "daily_quota_usd": DAILY_QUOTA_USD,
                "grace_remaining_seconds": round(grace, 1),
                "in_grace": grace > 0,
                "pnl_since_start": round(self.equity() - INITIAL_BANKROLL, 4),
            },
            "session": {
                "loops": self.loops,
                "hours_elapsed": round(elapsed / 3600, 4),
                "hours_remaining": None if remaining is None else round(remaining / 3600, 4),
                "stop_at": self.stop_at,
                "fills": fills,
                "realized_pnl": round(realized, 4),
                "open_positions": len(self.positions),
                "brain": brain_name(),
            },
            "time_remaining": {
                "grace_seconds": round(grace, 1),
                "seconds_to_next_quota_check": (
                    0.0 if grace > 0 else round(max(0.0, QUOTA_WINDOW_SECONDS - (now - self.last_quota_check)), 1)
                ),
            },
            "strategy": {
                name: {
                    "attempts": arm.attempts,
                    "wins": arm.wins,
                    "win_rate": round(arm.win_rate, 4),
                    "net_profits": round(arm.net_profit, 4),
                }
                for name, arm in self.arms.items()
            },
            "live_execution": self.live_execution,
            "kelly_fraction": KELLY_FRACTION,
            "ev_edge_min": EV_EDGE_MIN,
            "started_at": self.started_at,
            "last_quota_check": self.last_quota_check,
            "positions": {
                tid: {
                    "question": p.question,
                    "shares": p.shares,
                    "avg_price": p.avg_price,
                    "last_mid": p.last_mid,
                    "strategy": p.strategy,
                }
                for tid, p in self.positions.items()
            },
        }

    def write_log(self) -> None:
        LOG_JSON.write_text(json.dumps(self._snapshot(), indent=2), encoding="utf-8")

    def _load_log(self) -> None:
        if not LOG_JSON.exists():
            return
        try:
            data = json.loads(LOG_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if "started_at" in data:
            self.started_at = float(data["started_at"])
        if "last_quota_check" in data:
            self.last_quota_check = float(data["last_quota_check"])
        if "cash" in data and not self.positions:
            self.cash = float(data["cash"])

    # ----- risk -----------------------------------------------------------
    def equity(self) -> float:
        return self.cash + sum(p.shares * p.last_mid for p in self.positions.values())

    def flatten_all(self, reason: str) -> None:
        log.critical("FLATTEN: %s", reason)
        self.quotes.clear()
        for tid, pos in list(self.positions.items()):
            proceeds = pos.shares * pos.last_mid
            pnl = proceeds - pos.shares * pos.avg_price
            lr = log_return(pos.last_mid, pos.avg_price)
            self.cash += proceeds
            self.store.fill(token_id=tid, side="SELL", shares=pos.shares, price=pos.last_mid, pnl=pnl, log_return=lr, strategy=pos.strategy)
            self.store.delete(tid)
            del self.positions[tid]
            log.info("market-sold %s @ %.4f pnl=$%.4f logret=%.4f", tid[:12], pos.last_mid, pnl, lr)

    def die(self, reason: str) -> None:
        self.flatten_all(reason)
        self.write_log()
        log.critical("self-terminate: %s | final equity=$%.4f", reason, self.equity())
        sys.exit(1)

    def enforce_kill_switch(self) -> None:
        if self.equity() < KILL_BALANCE:
            self.die(f"equity ${self.equity():.4f} < kill ${KILL_BALANCE:.2f}")

    def enforce_quota(self) -> None:
        now = time.time()
        if now - self.started_at < GRACE_SECONDS or now - self.last_quota_check < QUOTA_WINDOW_SECONDS:
            return
        cutoff = now - QUOTA_WINDOW_SECONDS
        past = [m for m in self.equity_marks if m[0] <= cutoff]
        window_pnl = self.equity() - (past[-1][1] if past else INITIAL_BANKROLL)
        self.last_quota_check = now
        log.info("quota check: 24h pnl=$%.4f (need $%.2f)", window_pnl, DAILY_QUOTA_USD)
        if window_pnl < DAILY_QUOTA_USD:
            self.die(f"post-grace quota miss: 24h pnl ${window_pnl:.4f} < ${DAILY_QUOTA_USD}")

    def balance_ok(self, usd: float) -> bool:
        if usd < MIN_ORDER_USD or usd > self.cash:
            return False
        return True

    # ----- data -----------------------------------------------------------
    def fetch_liquid_markets(self) -> list[dict]:
        qs = urllib.parse.urlencode(
            {
                "closed": "false",
                "active": "true",
                "enable_order_book": "true",
                "limit": str(MARKET_SCAN_LIMIT),
                "order": "volume24hr",
                "ascending": "false",
            }
        )
        rows = http_json(f"{GAMMA_HOST}/markets?{qs}")
        if not isinstance(rows, list):
            return []
        out = []
        for m in rows:
            try:
                tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
            except (TypeError, json.JSONDecodeError, KeyError):
                continue
            if not tokens:
                continue
            m["_yes_token"] = str(tokens[0])
            m["_no_token"] = str(tokens[1]) if len(tokens) > 1 else ""
            m["_volume"] = _num(m.get("volume24hr") or m.get("volumeNum") or m.get("volume"))
            out.append(m)
        out.sort(key=lambda x: x["_volume"], reverse=True)
        return out

    def fetch_book(self, token_id: str) -> dict:
        client = self._clob_client()
        if client is not None:
            try:
                book = client.get_order_book(token_id)
                bids = [{"price": str(x.price), "size": str(x.size)} for x in (getattr(book, "bids", None) or [])]
                asks = [{"price": str(x.price), "size": str(x.size)} for x in (getattr(book, "asks", None) or [])]
                if bids or asks:
                    return {"bids": bids, "asks": asks, "last_trade_price": getattr(book, "last_trade_price", None)}
            except Exception as exc:
                log.warning("py-clob-client book fail (%s); REST fallback", exc)
        q = urllib.parse.urlencode({"token_id": token_id})
        return http_json(f"{CLOB_HOST}/book?{q}")

    def fetch_books(self, token_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        live = [t for t in token_ids if t not in self._dead_books]
        if not live:
            return out

        def one(tid: str) -> tuple[str, dict | None]:
            try:
                return tid, self.fetch_book(tid)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._dead_books.add(tid)
                    log.info("book 404 %s — skip rest of session", tid[:16])
                else:
                    log.warning("book skip %s: %s", tid[:16], exc)
                return tid, None

        with ThreadPoolExecutor(max_workers=BOOK_WORKERS) as pool:
            futs = [pool.submit(one, tid) for tid in live]
            for fut in as_completed(futs):
                tid, book = fut.result()
                if book:
                    out[tid] = book
        return out

    # ----- AI brain -------------------------------------------------------
    def gemini_probability(self, question: str, mid: float, spread: float) -> float | None:
        if time.time() < self._ai_cool_until:
            return None
        key = _gemini_key()
        if not key:
            return None
        prompt = PROMPT.format(question=question, mid=mid, spread=spread)
        payload = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 2048,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
        ).encode()
        headers = {"content-type": "application/json", "x-goog-api-key": key}
        last_exc: Exception | None = None
        for model, url, host in _gemini_chain():
            try:
                data = http_json(url, method="POST", body=payload, headers=headers, retry=False)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                exc.read()
                log.warning("gemini %s %s HTTP %s — next", model, host, exc.code)
                continue
            except Exception as exc:
                last_exc = exc
                log.warning("gemini %s %s failed: %s — next", model, host, exc)
                continue
            obj = _parse_json_object(_gemini_text(data))
            if not obj or "probability" not in obj:
                log.warning("gemini %s %s json miss — next", model, host)
                continue
            if model != GEMINI_MODEL or host != "vertex":
                log.info("gemini fallback model=%s via=%s", model, host)
            return _clamp(float(obj["probability"]), 0.01, 0.99)
        self._ai_cool_until = time.time() + AI_COOLDOWN_SECONDS
        log.warning("gemini chain exhausted — heuristic for %ss (%s)", AI_COOLDOWN_SECONDS, last_exc)
        return None

    def claude_probability(self, question: str, mid: float, spread: float) -> float | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        payload = json.dumps(
            {
                "model": CLAUDE_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": PROMPT.format(question=question, mid=mid, spread=spread)}],
            }
        ).encode()
        try:
            data = http_json(
                ANTHROPIC_HOST,
                method="POST",
                body=payload,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                retry=False,
            )
        except Exception as exc:
            log.warning("claude skip: %s", exc)
            return None
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        obj = _parse_json_object(text)
        if not obj or "probability" not in obj:
            log.warning("claude json miss: %s", text[:160])
            return None
        return _clamp(float(obj["probability"]), 0.01, 0.99)

    def ai_probability(self, question: str, mid: float, spread: float) -> float | None:
        if _gemini_key():
            p = self.gemini_probability(question, mid, spread)
            if p is not None:
                return p
        return self.claude_probability(question, mid, spread)

    # ----- bandit ---------------------------------------------------------
    def pick_strategy(self) -> str:
        # Exploit average PnL, not win-rate, so a 1/13 scalper does not dominate.
        if random.random() < EPSILON:
            return random.choice(BANDIT_ARMS)
        return max(BANDIT_ARMS, key=lambda s: self.arms[s].net_profit / max(self.arms[s].attempts, 1))

    def record_outcome(self, strategy: str, pnl: float) -> None:
        arm = self.arms[strategy]
        arm.attempts += 1
        arm.net_profit += pnl
        if pnl > 0:
            arm.wins += 1

    # ----- execution (paper GTC) ------------------------------------------
    def paper_buy(self, token_id: str, question: str, usd: float, price: float, strategy: str, prior_p: float) -> None:
        if token_id in self.positions or len(self.positions) >= MAX_POSITIONS:
            return
        if not self.balance_ok(usd) or price <= 0:
            return
        shares = usd / price
        self.cash -= usd
        pos = self.positions.get(token_id)
        if pos:
            n = pos.shares + shares
            pos.avg_price = (pos.avg_price * pos.shares + price * shares) / n
            pos.shares, pos.last_mid, pos.strategy, pos.prior_p = n, price, strategy, prior_p
        else:
            pos = Position(token_id, question, shares, price, price, strategy, prior_p, time.time())
            self.positions[token_id] = pos
        self.store.upsert(pos)
        self.store.fill(token_id=token_id, side="BUY", shares=shares, price=price, pnl=0.0, log_return=0.0, strategy=strategy)
        log.info("[%s] GTC FILL BUY $%.2f @ %.3f  %s", strategy, usd, price, question[:70])

    def paper_sell(self, token_id: str, bid: float, strategy: str) -> None:
        pos = self.positions.get(token_id)
        if not pos or bid <= 0:
            return
        proceeds = pos.shares * bid
        pnl = proceeds - pos.shares * pos.avg_price
        lr = log_return(bid, pos.avg_price)
        self.cash += proceeds
        self.record_outcome(strategy, pnl)
        self.store.fill(token_id=token_id, side="SELL", shares=pos.shares, price=bid, pnl=pnl, log_return=lr, strategy=strategy)
        self.store.delete(token_id)
        del self.positions[token_id]
        log.info("[%s] SELL @ %.3f pnl=$%.4f logret=%.4f", strategy, bid, pnl, lr)

    def size_usd(self, p: float, price: float) -> float:
        usd = min(quarter_kelly(p, price) * self.equity(), CLIP_USD)
        if usd < MIN_ORDER_USD:
            return 0.0
        return min(usd, self.cash)

    def too_thin(self, bid: float, ask: float, mid: float) -> bool:
        return mid > 0 and (ask - bid) / mid > MAX_SLIPPAGE

    def _tradeable(self, r: dict[str, Any]) -> bool:
        mid = r["mid"]
        if not (PRICE_LO <= mid <= PRICE_HI):
            return False
        if r["spread"] <= 0 or r["spread"] > 0.08:
            return False
        if r.get("bid_sz", 0) < MIN_TOP_SIZE or r.get("ask_sz", 0) < MIN_TOP_SIZE:
            return False
        if r.get("volume", 0) < MIN_VOLUME_24H:
            return False
        return True

    # ----- strategies -----------------------------------------------------
    def _history(self, token_id: str, mid: float) -> list[float]:
        hist = self.mid_history.setdefault(token_id, [])
        hist.append(mid)
        del hist[:-40]
        return hist

    def manage_exits(self, token_id: str, bid: float, ask: float, mid: float) -> None:
        pos = self.positions.get(token_id)
        if not pos or bid <= 0:
            return
        edge = bid - pos.avg_price
        # Stop immediately. Do not wait 2 minutes while the bid gaps through.
        if edge <= -STOP_LOSS:
            self.quotes = [q for q in self.quotes if q.token_id != token_id]
            self.paper_sell(token_id, bid, pos.strategy)
            return
        # Spread wins by selling the ask, not by hitting the bid.

    def act_spread(self, token_id: str, question: str, bid: float, ask: float, mid: float, bid_sz: float, ask_sz: float) -> None:
        spread = ask - bid
        if spread < WIDE_SPREAD or spread > 0.06:
            return
        if bid_sz < MIN_TOP_SIZE or ask_sz < MIN_TOP_SIZE:
            return
        if token_id in self.positions or any(q.token_id == token_id for q in self.quotes):
            return
        usd = min(CLIP_USD, self.cash)
        if not self.balance_ok(usd):
            return
        # Join the bid. Fill only if last trade prints through that price.
        self.quotes.append(RestingQuote(token_id, question, "BUY", round(bid, 4), usd, "SPREAD_SCALPER", time.time()))

    def act_arb(self, r: dict[str, Any]) -> None:
        """Buy YES+NO when their asks sum to less than $1. Pair redeems at $1."""
        yes_ask = r["ask"]
        no_ask = r.get("no_ask")
        if no_ask is None:
            return
        edge = 1.0 - (yes_ask + no_ask)
        if edge < ARB_EDGE:
            return
        last = self._arb_taken.get(r["token_id"], 0)
        if time.time() - last < 900:
            return
        if min(r.get("ask_sz", 0), r.get("no_ask_sz", 0)) < 5:
            return
        shares = min(CLIP_USD / yes_ask, CLIP_USD / no_ask, r["ask_sz"], r["no_ask_sz"])
        cost = shares * (yes_ask + no_ask)
        if cost < MIN_ORDER_USD or cost > self.cash:
            return
        pnl = shares - cost
        self.cash += pnl
        self._arb_taken[r["token_id"]] = time.time()
        self.record_outcome("COMPLEMENT_ARB", pnl)
        self.store.fill(
            token_id=r["token_id"],
            side="ARB",
            shares=shares,
            price=yes_ask + no_ask,
            pnl=pnl,
            log_return=0.0,
            strategy="COMPLEMENT_ARB",
        )
        log.info("[COMPLEMENT_ARB] pair $%.3f+$%.3f edge=%.3f pnl=$%.4f  %s", yes_ask, no_ask, edge, pnl, r["question"][:60])

    def act_directional(
        self,
        token_id: str,
        question: str,
        bid: float,
        ask: float,
        mid: float,
        strategy: str,
        p: float,
    ) -> None:
        hist = self.mid_history.get(token_id, [])
        if len(hist) < TREND_BARS:
            return
        ret = hist[-1] - hist[-TREND_BARS]
        up_steps = sum(1 for i in range(1, 4) if hist[-i] >= hist[-i - 1])
        if strategy == "TREND_RIDER":
            if token_id in self.positions:
                return
            if ret < TREND_MOVE or up_steps < 3:
                return
        elif strategy == "MEAN_REVERSION":
            if token_id in self.positions:
                return
            if ret > -REVERSION_SPIKE:
                return
            # Only buy a dip if our p still thinks YES is underpriced.
        else:
            return
        ev = expected_value(p, ask)
        if ev < EV_EDGE_MIN:
            return
        if self.too_thin(bid, ask, mid):
            return
        usd = self.size_usd(p, ask)
        if usd < MIN_ORDER_USD or any(q.token_id == token_id for q in self.quotes):
            return
        self.quotes.append(RestingQuote(token_id, question, "BUY", round(ask, 4), usd, strategy, time.time()))

    def fill_resting_quotes(self, token_id: str, last_trade: float | None, ask: float) -> None:
        kept: list[RestingQuote] = []
        now = time.time()
        for q in self.quotes:
            if now - q.created_at > 180:
                continue
            if q.token_id != token_id:
                kept.append(q)
                continue
            # Real print only. Never fill against ask as a proxy — that was the fake-fill bug.
            if q.side == "BUY" and last_trade is not None and last_trade <= q.price:
                self.paper_buy(q.token_id, q.question, q.usd, q.price, q.strategy, self.priors.get(token_id, q.price))
                if q.strategy == "SPREAD_SCALPER" and q.token_id in self.positions and ask > q.price:
                    self.quotes.append(
                        RestingQuote(q.token_id, q.question, "SELL", round(ask, 4), q.usd, q.strategy, time.time())
                    )
            elif q.side == "SELL" and last_trade is not None and last_trade >= q.price:
                self.paper_sell(q.token_id, q.price, q.strategy)
            else:
                kept.append(q)
        self.quotes = kept[-32:]

    # ----- main loop ------------------------------------------------------
    def step(self) -> None:
        if self.live_execution:
            self.die("live_execution=True is not supported")

        markets = self.fetch_liquid_markets()
        if not markets:
            log.info("no markets returned; sleeping")
            return

        strategy = self.pick_strategy()

        ids = []
        for m in markets:
            ids.append(m["_yes_token"])
            if m.get("_no_token"):
                ids.append(m["_no_token"])
        books = self.fetch_books(ids)
        rows: list[dict[str, Any]] = []
        for m in markets:
            token_id = m["_yes_token"]
            book = books.get(token_id)
            if not book:
                continue
            bid, ask = _best_bid_ask(book)
            last = book.get("last_trade_price")
            last_f = float(last) if last not in (None, "") else None
            mid = _mid(bid, ask, last_f)
            if bid is None or ask is None or mid is None:
                continue
            no_bid = no_ask = no_ask_sz = None
            nob = books.get(m.get("_no_token") or "")
            if nob:
                no_bid, no_ask = _best_bid_ask(nob)
                no_ask_sz = _top_size(nob.get("asks"))
            rows.append(
                {
                    "token_id": token_id,
                    "question": m.get("question") or m.get("slug") or token_id,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread": ask - bid,
                    "last_f": last_f,
                    "bid_sz": _top_size(book.get("bids")),
                    "ask_sz": _top_size(book.get("asks")),
                    "volume": m.get("_volume", 0.0),
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "no_ask_sz": no_ask_sz or 0.0,
                }
            )

        tradeable = [r for r in rows if self._tradeable(r)]
        ai_p_map: dict[str, float | None] = {}
        if brain_name() != "heuristic" and tradeable and time.time() >= self._ai_cool_until:
            for r in tradeable[:AI_SCAN_LIMIT]:
                if time.time() < self._ai_cool_until:
                    break
                ai_p_map[r["token_id"]] = self.ai_probability(r["question"], r["mid"], r["spread"])

        log.info("arm=%s scan=%d tradeable=%d equity=$%.4f", strategy, len(markets), len(tradeable), self.equity())

        for r in rows:
            token_id, question, bid, ask, mid = r["token_id"], r["question"], r["bid"], r["ask"], r["mid"]
            self._history(token_id, mid)
            if token_id in self.positions:
                self.positions[token_id].last_mid = mid
                self.store.upsert(self.positions[token_id])

            prior = self.priors.get(token_id, mid)
            p = bayes_update(prior, mid)
            ai_p = ai_p_map.get(token_id)
            if ai_p is not None:
                p = bayes_update(p, ai_p, strength=0.50)
            self.priors[token_id] = p
            if token_id in self.positions:
                self.positions[token_id].prior_p = p

            self.fill_resting_quotes(token_id, r["last_f"], ask)
            self.manage_exits(token_id, bid, ask, mid)
            self.act_arb(r)

            if not self._tradeable(r) or len(self.positions) >= MAX_POSITIONS:
                continue
            if strategy == "SPREAD_SCALPER":
                self.act_spread(token_id, question, bid, ask, mid, r["bid_sz"], r["ask_sz"])
            elif ai_p is None:
                continue
            else:
                self.act_directional(token_id, question, bid, ask, mid, strategy, p)

        self.loops += 1

        self.equity_marks.append((time.time(), self.equity()))
        self.equity_marks = [(t, e) for t, e in self.equity_marks if t >= time.time() - 3 * QUOTA_WINDOW_SECONDS]
        self.enforce_kill_switch()
        self.enforce_quota()
        self.write_log()

    def run(self, once: bool = False, hours: float | None = None) -> None:
        asyncio.run(self.run_async(once=once, hours=hours))

    async def run_async(self, once: bool = False, hours: float | None = None) -> None:
        if hours and hours > 0:
            self.stop_at = time.time() + hours * 3600
        log.info(
            "paper start cash=$%.2f live=%s brain=%s hours=%s kelly=%.2f",
            self.cash,
            self.live_execution,
            brain_name(),
            hours,
            KELLY_FRACTION,
        )
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        self.write_log()
        try:
            while True:
                try:
                    self.step()
                except SystemExit:
                    raise
                except Exception:
                    log.exception("loop fault — cooldown")
                    await asyncio.sleep(NETWORK_COOLDOWN_SECONDS)
                if once:
                    return
                if self.stop_at and time.time() >= self.stop_at:
                    log.info("session complete loops=%d equity=$%.4f", self.loops, self.equity())
                    self.write_log()
                    return
                await asyncio.sleep(LOOP_SECONDS)
        finally:
            try:
                PID_PATH.unlink()
            except OSError:
                pass


def _self_check() -> None:
    assert abs(expected_value(0.60, 0.50) - 0.10) < 1e-9
    assert expected_value(0.52, 0.50) < EV_EDGE_MIN
    assert abs(quarter_kelly(0.60, 0.50) - 0.05) < 1e-9  # 0.25 * 0.20
    assert quarter_kelly(0.40, 0.50) == 0.0
    assert abs(bayes_update(0.50, 0.70, 0.30) - 0.56) < 1e-9
    assert abs(log_return(0.44, 0.40) - math.log(1.1)) < 1e-9
    bot = PayOrDieBot.__new__(PayOrDieBot)
    bot.cash, bot.positions, bot.quotes = 50.0, {}, []
    assert abs(bot.equity() - 50.0) < 1e-9
    good = {"mid": 0.50, "spread": 0.03, "bid_sz": 100, "ask_sz": 100, "volume": 5000}
    assert bot._tradeable(good)
    assert not bot._tradeable({**good, "mid": 0.95})
    assert not bot._tradeable({**good, "volume": 10})
    fake = {"candidates": [{"content": {"parts": [{"text": '{"probability": 0.42, "confidence": "low", "reasoning": "x"}'}]}}]}
    assert abs(_clamp(float(_parse_json_object(_gemini_text(fake))["probability"]), 0.01, 0.99) - 0.42) < 1e-9
    wrapped = _parse_json_object('Here:\n```json\n{"probability": 0.33, "confidence": "low", "reasoning": "t"}\n```')
    assert abs(wrapped["probability"] - 0.33) < 1e-9
    assert abs((1.0 - 0.48 - 0.50) - 0.02) < 1e-9
    cool = PayOrDieBot.__new__(PayOrDieBot)
    cool._ai_cool_until = time.time() + 999
    assert cool.gemini_probability("x", 0.5, 0.02) is None
    chain = _gemini_chain()
    assert chain[0][0] == GEMINI_MODEL and chain[0][2] == "vertex"
    assert any(m == "gemini-3.8-flash" and h == "aistudio" for m, _, h in chain)
    dead = PayOrDieBot.__new__(PayOrDieBot)
    dead._dead_books = {"404token"}
    dead._clob = None
    assert dead.fetch_books(["404token"]) == {}
    print("self-check ok")


def _probe() -> None:
    bot = PayOrDieBot()
    name = brain_name()
    if name == "heuristic":
        print("no GEMINI_API_KEY / GOOGLE_API_KEY / ANTHROPIC_API_KEY in env or .env")
        raise SystemExit(1)
    p = bot.ai_probability("Will a fair coin land heads?", 0.50, 0.02)
    print(json.dumps({"brain": name, "probability": p, "ok": p is not None}, indent=2))
    raise SystemExit(0 if p is not None else 2)


def _status() -> None:
    if not LOG_JSON.exists():
        print("no paper_trade_log.txt yet")
        raise SystemExit(1)
    print(LOG_JSON.read_text(encoding="utf-8"))
    if PID_PATH.exists():
        print(f"\n# pid {PID_PATH.read_text().strip()} (running)", file=sys.stderr)
    raise SystemExit(0)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--hours", type=float, default=None)
    args = p.parse_args()
    if args.check:
        _self_check()
        raise SystemExit(0)
    if args.probe:
        _probe()
    if args.status:
        _status()
    bot = PayOrDieBot()
    if args.fresh:
        bot.reset_paper()
    bot.run(once=args.once, hours=args.hours)
