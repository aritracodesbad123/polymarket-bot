"""Autonomous paper loop. Live only if every lock is open."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.ai.probability_engine import InvalidEstimate, ProbabilityEngine
from app.ai.prompt_manager import PromptManager
from app.ai.grok_client import GrokClient
from app.ai.gemini_client import GeminiClient
from app.ai.credits import CreditHardFail
from app.broker.live import LiveBroker
from app.broker.paper import PaperBroker
from app.config import Settings
from app.evaluation.reports import daily_report
from app.execution.executor import Executor, idempotency_key
from app.market_data.client import PolymarketClient
from app.market_data.scanner import (
    MID_OUTSIDE_BAND,
    MarketScanner,
    filter_book,
    filter_market,
    filter_tradeable_mid,
)
from app.monitoring.logging import setup_logging
from app.monitoring.telegram import Telegram
from app.portfolio.portfolio import Portfolio
from app.research.researcher import EvidencePacket, NullResearchProvider, XAISearchProvider
from app.risk.manager import RiskManager, exposure_from_positions
from app.risk.regime import RegimeEngine, new_screening_allowed
from app.storage.db import Database, DatabaseError
from app.storage.repositories import Repositories
from app.strategy.evaluator import Decision, StrategyEvaluator
from app.strategy.holding_review import diagnose_holding, thesis_from_decision

BANNER_PAPER = """
========================================
POLYGROK TRADING BOT
MODE: PAPER
========================================
""".strip()

BANNER_LIVE = """
========================================
POLYGROK TRADING BOT
MODE: LIVE CANARY
========================================
""".strip()

BANNER_HALTED = """
========================================
POLYGROK TRADING BOT
MODE: HALTED
========================================
""".strip()

# Survival lock while on Gemini (Max/John): +2¢ edge, half Kelly, higher conf, fee-aware floor.
GEMINI_EDGE_TIGHTEN = 0.02
GEMINI_KELLY_MULTIPLIER = 0.125
GEMINI_MIN_CONFIDENCE = 0.50
GEMINI_MIN_EXEC_EDGE = 0.02



def _instrument_label(m) -> str:
    """Human ticker-ish label for console: BTC / ETH / EURUSD / XAU ..."""
    blob = f"{getattr(m, 'question', '')} {getattr(m, 'category', '')}".lower()
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
        ("USDARS", ("usd to ars", "ars", "argentina")),
    )
    for label, keys in rules:
        if any(k.strip() in blob for k in keys):
            return label
    cat = (getattr(m, "category", "") or "").strip()
    if cat and cat.lower() not in ("other", ""):
        return cat.upper()[:16]
    # last resort: first 24 chars of question
    q = (getattr(m, "question", "") or "").strip()
    return (q[:24] + "…") if len(q) > 24 else (q or "UNKNOWN")


def _spread_bucket(spread: float) -> str:
    """Bucket rejected spreads for console/KPI (cents on a 0-1 book)."""
    c = spread * 100.0
    if c <= 6:
        return "le_6c"
    if c <= 10:
        return "7_10c"
    if c <= 15:
        return "11_15c"
    return "gt_15c"


def _soft_ai_reject(reason: str) -> bool:
    """Cooldown-miss / cooldown / cascade exhaustion should abstain, not trip AI kill."""
    r = (reason or "").lower()
    return any(
        s in r
        for s in (
            "gemini_cooldown",
            "gemini_cascade_exhausted",
            "gemini_error:gemini_cooldown",
            "parse miss",
            "malformed_gemini_output",
            "xai_credits_exhausted",
            "no_provider",
        )
    )



def banner_for(settings: Settings, repo: Repositories) -> str:
    st = repo.state()
    if st.halted:
        return BANNER_HALTED
    live = LiveBroker(settings, repo)
    if live.authorize_now().allowed:
        return BANNER_LIVE
    return BANNER_PAPER


class TradingApp:
    def __init__(self, settings: Settings, db_path: str | None = None) -> None:
        self.settings = settings
        self.log = setup_logging()
        self.db = Database(db_path or settings.db_path)
        self.repo = Repositories(self.db)
        self.data = PolymarketClient(
            settings.polymarket_gamma_url,
            settings.polymarket_api_url,
            settings.polymarket_ws_url,
        )
        self.scanner = MarketScanner(self.data, settings)
        self.paper = PaperBroker(settings.paper_starting_bankroll, settings.paper_latency_ms)
        self.live = LiveBroker(settings, self.repo)
        self.executor = Executor(settings, self.repo, self.paper, self.live, self.data)
        self.portfolio = Portfolio(self.paper, self.repo)
        self.portfolio.hydrate_paper(settings.paper_starting_bankroll)
        self.risk = RiskManager(settings, self.repo)
        self.regime = RegimeEngine(settings, self.repo)
        self.strategy = StrategyEvaluator(settings)
        self.telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        self.stop = False
        self.cycle_stats = {
            "candidates": 0,
            "rejected": 0,
            "avg_edge": 0.0,
            "avg_exec_edge": 0.0,
            "avg_slip": 0.0,
            "partials": 0,
            "ai_errors": 0,
        }
        self._pending_ai_provider_notice: str | None = None
        grok = (
            GrokClient(settings.xai_api_key, settings.grok_model)
            if settings.xai_api_key
            else None
        )
        gemini = GeminiClient(settings.gemini_api_key) if settings.gemini_api_key else None
        if grok or gemini:
            self.engine: ProbabilityEngine | None = ProbabilityEngine(
                grok,
                PromptManager(settings.prompt_version),
                gemini,
                on_provider_swap=self._on_provider_swap,
            )
        else:
            self.engine = None
        if grok is not None:
            self.research = XAISearchProvider(settings.xai_api_key, settings.grok_model)
        else:
            self.research = NullResearchProvider()

    async def start_clock(self) -> None:
        self.repo.mark_paper_started()
        self.repo.event("STARTED", "polygrok started")
        try:
            body = (PromptManager(self.settings.prompt_version).body)
            self.repo.upsert_prompt(self.settings.prompt_version, body)
        except Exception:
            pass

    async def notify(self, text: str) -> None:
        self.log.info(text.replace("\n", " | "))
        try:
            await self.telegram.send(text)
        except Exception:
            self.repo.event("API_ERROR", "telegram")

    def _on_provider_swap(self, model: str) -> None:
        msg = f"AI_PROVIDER gemini:vertex model={model} (xAI credits exhausted)"
        self._pending_ai_provider_notice = msg
        self.repo.event("AI_PROVIDER", msg)

    async def cycle(self) -> None:
        self.risk.enforce_daily_realized_cap()
        st = self.repo.state()
        if st.halted:
            return
        # Exits stay on this path. Session-budget DIE and the post-fill
        # screening stop only skip the universe scan and new AI calls below.
        marks: dict[str, float] = {}
        await self._review_holdings(marks)
        unreal = 0.0
        for p in self.paper._positions.values():
            if p.shares <= 0:
                continue
            mpx = marks.get(p.token_id, p.avg_price)
            unreal += p.shares * (mpx - p.avg_price)
        equity = self.paper.equity(marks)
        regime = self.regime.update(equity=equity, unrealized_pnl=unreal)
        self.log.info(
            "REGIME mode=%s reason=%s burn=%.4f screening=%s fills=%s",
            regime.mode,
            regime.reason,
            regime.session_ai_cost_usd,
            regime.screening_allowed,
            regime.has_taken_fills,
        )
        prev = getattr(self, "_last_regime_mode", None)
        if prev != regime.mode or getattr(self, "_last_screening", None) != regime.screening_allowed:
            self.repo.event("REGIME", f"{regime.mode} | {regime.reason}", {
                "mode": regime.mode,
                "ai_calls": regime.session_ai_calls,
                "ai_cost": regime.session_ai_cost_usd,
                "equity": equity,
                "screening_allowed": regime.screening_allowed,
                "has_taken_fills": regime.has_taken_fills,
            })
            self._last_regime_mode = regime.mode
            self._last_screening = regime.screening_allowed

        if new_screening_allowed(regime):
            await self._screen_new_markets(marks)
        else:
            self.cycle_stats["candidates"] = 0
        self.portfolio.snapshot(marks)
        eq = self.paper.equity(marks)
        self.risk.daily_pnl = eq - self.settings.paper_starting_bankroll
        self.risk.note_equity(eq, self.settings.paper_starting_bankroll)
        mismatch = await self.paper.reconcile()
        if mismatch:
            self.risk.note_recon_mismatch(mismatch)

    async def _screen_new_markets(self, marks: dict[str, float]) -> None:
        self.cycle_stats["candidates"] = 0
        try:
            scanned = await self.scanner.scan()
            self.risk.note_api_ok()
        except Exception as exc:
            self.risk.note_api_failure()
            self.repo.event("API_ERROR", str(exc))
            return
        candidates = []
        for m, reason in scanned:
            self.repo.upsert_market(
                {
                    "market_id": m.market_id,
                    "condition_id": m.condition_id,
                    "yes_token_id": m.yes_token_id,
                    "no_token_id": m.no_token_id,
                    "question": m.question,
                    "description": m.description,
                    "resolution_criteria": m.resolution_criteria,
                    "close_time": m.close_time.isoformat() if m.close_time else None,
                    "resolution_time": m.resolution_time.isoformat() if m.resolution_time else None,
                    "category": m.category,
                    "event_id": m.event_id,
                    "correlation_group": m.correlation_group,
                    "neg_risk": m.neg_risk,
                    "tick_size": str(m.tick_size),
                    "min_order_size": str(m.min_order_size),
                    "status": m.status,
                    "raw": m.raw,
                }
            )
            if reason is None:
                candidates.append(m)
        self.cycle_stats["candidates"] = len(candidates)
        grok_calls = 0
        max_calls = self.regime.max_ai_calls()
        if max_calls <= 0:
            return
        positions = await self.paper.positions()
        for m in candidates:
            if grok_calls >= max_calls:
                break
            if self.repo.state().halted:
                break
            used = await self._consider(m, positions, marks)
            if used:
                grok_calls += 1
                self.regime.note_ai_call(1)
            last = self.regime.last
            burn = self.regime.session_ai_cost_usd
            budget = self.settings.ai_session_budget_usd
            if (
                last is not None
                and not last.has_taken_fills
                and budget > 0
                and burn >= budget
            ):
                break
            gemini_ready = bool(self.engine and self.engine.gemini)
            grok_dead = bool(
                getattr(self.research, "blocked", False)
                or (self.engine and self.engine.grok and self.engine.grok.blocked)
            )
            if grok_dead and not gemini_ready:
                break

    async def _books(self, m):
        yes_book = await self.data.get_order_book(m.yes_token_id, m.market_id)
        no_book = (
            await self.data.get_order_book(m.no_token_id, m.market_id)
            if m.no_token_id
            else None
        )
        return yes_book, no_book

    def _mark_books(self, m, yes_book, no_book, marks: dict[str, float]) -> None:
        if yes_book.best_ask:
            marks[m.yes_token_id] = yes_book.best_ask
            m.yes_price = yes_book.best_ask
        if no_book and no_book.best_ask and m.no_token_id:
            marks[m.no_token_id] = no_book.best_ask
            m.no_price = no_book.best_ask

    def _skip_xai_search(self) -> bool:
        if getattr(self.research, "blocked", False):
            return True
        return bool(self.engine and self.engine.provider == "gemini")

    def _provider_tag(self) -> str:
        if not self.engine:
            return "none"
        prov = self.engine.provider or "none"
        model = self.engine.last_model or ""
        return f"{prov}:{model}" if model else prov

    def _gemini_strategy_kwargs(self) -> dict:
        """Stricter gates while on Gemini; human reset when back on Grok."""
        if not (self.engine and self.engine.provider == "gemini"):
            return {}
        return {
            "min_edge": self.settings.min_edge + GEMINI_EDGE_TIGHTEN,
            "kelly_multiplier": GEMINI_KELLY_MULTIPLIER,
            "min_confidence_score": GEMINI_MIN_CONFIDENCE,
            "min_exec_edge": GEMINI_MIN_EXEC_EDGE,
        }

    def _strategy_kwargs(self) -> dict:
        kw = dict(self._gemini_strategy_kwargs())
        rk = self.regime.evaluate_kwargs()
        if not rk:
            return kw
        s = self.settings
        if "min_edge" in rk:
            kw["min_edge"] = max(kw.get("min_edge", s.min_edge), rk["min_edge"])
        if "kelly_multiplier" in rk:
            kw["kelly_multiplier"] = min(
                kw.get("kelly_multiplier", s.kelly_multiplier),
                rk["kelly_multiplier"],
            )
        return kw

    async def _review_holdings(self, marks: dict[str, float]) -> None:
        """Diagnose open tickets; paper-SELL when thesis/stop/time says exit."""
        positions = list(self.paper._positions.values())
        for pos in positions:
            if pos.shares <= 1e-12:
                continue
            try:
                book = await self.data.get_order_book(pos.token_id, pos.market_id)
            except Exception as exc:
                self.risk.note_api_failure()
                self.repo.event("API_ERROR", f"holding_book:{exc}")
                continue
            if book.best_bid:
                marks[pos.token_id] = book.best_bid
            elif book.midpoint:
                marks[pos.token_id] = book.midpoint
            row = self.repo.latest_approved_decision(pos.token_id)
            entry_p, entry_ts = thesis_from_decision(row)
            verdict = diagnose_holding(
                shares=pos.shares,
                avg_price=pos.avg_price,
                token_id=pos.token_id,
                market_id=pos.market_id,
                book=book,
                entry_p=entry_p,
                entry_ts=entry_ts,
                settings=self.settings,
            )
            if verdict.reason == "ok":
                continue
            stale = filter_book(book, self.settings)
            if stale and stale != "spread_too_wide":
                self.repo.event(
                    "HOLDING_HOLD",
                    f"{verdict.reason}|{stale}|{pos.token_id[:12]}",
                    {"reason": verdict.reason, "book": stale},
                )
                continue
            if book.best_bid is None or book.bid_depth_usd() <= 0:
                continue
            limit = float(book.best_bid)
            exit_key = idempotency_key(
                pos.market_id,
                pos.token_id,
                "SELL",
                self.settings.strategy_version,
                kind="exit",
            )
            if self.repo.get_decision_by_idempotency(exit_key) or self.repo.get_order_by_idempotency(exit_key):
                continue
            decision = Decision(
                approved=True,
                reject_reason=None,
                gates=[],
                market_id=pos.market_id,
                token_id=pos.token_id,
                side="SELL",
                grok_p=entry_p,
                market_price=limit,
                size_shares=pos.shares,
                size_usd=pos.shares * limit,
                limit_price=limit,
                category=pos.category,
                correlation_group=pos.correlation_group,
            )
            did = self.repo.insert_decision(
                {
                    "market_id": pos.market_id,
                    "token_id": pos.token_id,
                    "side": "SELL",
                    "approved": True,
                    "reject_reason": None,
                    "gates": {"exit_reason": {"passed": True, "detail": verdict.reason}},
                    "grok_p": entry_p,
                    "market_price": limit,
                    "raw_edge": verdict.edge_now,
                    "size_usd": decision.size_usd,
                    "size_shares": decision.size_shares,
                    "strategy_version": self.settings.strategy_version,
                    "prompt_version": self.settings.prompt_version,
                    "idempotency_key": exit_key,
                }
            )
            before_pnl = self.paper.realized_pnl
            err = await self.executor.execute(decision, did, kind="exit")
            if err:
                self.repo.event("HOLDING_EXIT_FAIL", f"{verdict.reason}|{err}")
                continue
            delta = self.paper.realized_pnl - before_pnl
            self.risk.note_realized_pnl(delta)
            if delta >= 0:
                self.risk.note_win()
            else:
                self.risk.note_loss()
            self.log.info(
                "HOLDING_EXIT reason=%s token=%s pnl=%.4f",
                verdict.reason,
                pos.token_id[:16],
                delta,
            )
            self.repo.event(
                "HOLDING_EXIT",
                f"{verdict.reason} | pnl={delta:.4f}",
                {"reason": verdict.reason, "detail": verdict.detail, "pnl": delta},
            )

    async def _consider(self, m, positions, marks: dict[str, float]) -> bool:
        if not m.yes_token_id:
            return False
        try:
            yes_book, no_book = await self._books(m)
        except Exception as exc:
            self.risk.note_api_failure()
            self.repo.event("API_ERROR", str(exc))
            return False
        self._mark_books(m, yes_book, no_book, marks)
        book_reject = filter_book(yes_book, self.settings)
        if book_reject:
            # Only real staleness feeds the repeated_stale_data kill — spread/empty
            # rejects must not inflate that counter (false halt under Survival Mode).
            if book_reject == "stale_data":
                self.risk.note_stale()
            extra = {}
            if book_reject == "spread_too_wide" and yes_book.spread is not None:
                extra = {
                    "spread": float(yes_book.spread),
                    "max_spread": float(self.settings.max_spread),
                    "best_bid": yes_book.best_bid,
                    "best_ask": yes_book.best_ask,
                }
            self._reject(m, book_reject, extra=extra)
            return False
        self.risk.note_book_fresh()
        self.repo.insert_book(
            m.market_id,
            m.yes_token_id,
            [x.model_dump() for x in yes_book.bids],
            [x.model_dump() for x in yes_book.asks],
            str(yes_book.tick_size),
            str(yes_book.min_order_size),
            yes_book.neg_risk,
            yes_book.hash,
        )
        self.repo.insert_snapshot(
            m.market_id,
            {
                "yes_price": yes_book.best_ask,
                "no_price": no_book.best_ask if no_book else None,
                "midpoint": yes_book.midpoint,
                "spread": yes_book.spread,
                "volume": m.volume,
                "liquidity": m.liquidity,
                "best_bid": yes_book.best_bid,
                "best_ask": yes_book.best_ask,
            },
        )
        # Fresh book mid is in. Lottery / near-certain books never reach research
        # or engine.estimate, and _consider returns False so the session burn
        # counter does not move.
        mid_reject = filter_tradeable_mid(yes_book.midpoint, self.settings)
        if mid_reject:
            self._reject(
                m,
                mid_reject,
                extra={
                    "mid": yes_book.midpoint,
                    "min_tradeable_mid": self.settings.min_tradeable_mid,
                    "max_tradeable_mid": self.settings.max_tradeable_mid,
                },
            )
            return False
        packet = EvidencePacket(
            market_id=m.market_id,
            question=m.question,
            resolution_criteria=m.resolution_criteria or m.description,
            market_price=yes_book.best_ask,
            implied_probability=yes_book.midpoint,
        )
        if not self._skip_xai_search():
            try:
                packet = await self.research.gather(packet)
            except CreditHardFail as exc:
                self.repo.event("GROK_ERROR", f"research:{exc}")
                if self.engine and self.engine.grok:
                    self.engine.grok.blocked = True
            except Exception as exc:
                self.repo.event("GROK_ERROR", f"research:{exc}")
                self._reject(m, "research_failed")
                return True
        evid_id = self.repo.insert_evidence(m.market_id, packet.model_dump(mode="json"))
        if self.engine is None:
            self._reject(m, "no_xai_key")
            return True
        try:
            est = await self.engine.estimate(packet)
        except InvalidEstimate as exc:
            self.repo.event("GROK_ERROR", exc.reason)
            # Soft Gemini rejects (cooldown / cascade parse-miss) must not inflate
            # repeated_ai_errors — same class of bug as counting spread as stale.
            if not _soft_ai_reject(exc.reason):
                self.cycle_stats["ai_errors"] += 1
                self.risk.note_ai_error(self.cycle_stats["ai_errors"])
            self._reject(m, exc.reason)
            return True
        # Successful estimate clears hard AI-error streak.
        self.cycle_stats["ai_errors"] = 0
        if self._pending_ai_provider_notice:
            await self.notify(self._pending_ai_provider_notice)
            self._pending_ai_provider_notice = None
        pred_id = self.repo.insert_prediction(
            {
                "market_id": m.market_id,
                "prompt_version": self.settings.prompt_version,
                "model": self._provider_tag(),
                "estimated_probability": est.estimated_probability,
                "confidence": est.confidence,
                "confidence_score": est.confidence_score,
                "should_abstain": est.should_abstain,
                "estimate_json": est.model_dump(),
                "evidence_id": evid_id,
            }
        )
        try:
            yes_book, no_book = await self._books(m)
        except Exception as exc:
            self.risk.note_api_failure()
            self.repo.event("API_ERROR", str(exc))
            return True
        self._mark_books(m, yes_book, no_book, marks)
        exp = exposure_from_positions(positions, marks)
        canary = self.live.authorize_now().allowed
        gemini_kw = self._strategy_kwargs()
        for book in (yes_book, no_book):
            if book is None:
                continue
            reason = filter_book(book, self.settings)
            if reason:
                if reason == "stale_data":
                    self.risk.note_stale()
                extra = {}
                if reason == "spread_too_wide" and book.spread is not None:
                    extra = {
                        "spread": float(book.spread),
                        "max_spread": float(self.settings.max_spread),
                        "best_bid": book.best_bid,
                        "best_ask": book.best_ask,
                    }
                self._reject(m, reason, extra=extra)
                continue
            key = idempotency_key(
                m.market_id,
                book.token_id,
                "BUY",
                self.settings.strategy_version,
            )
            duplicate = self.repo.get_order_by_idempotency(key) is not None
            decision = self.strategy.evaluate(
                market=m,
                book=book,
                estimate=est,
                packet=packet,
                bankroll=self.settings.paper_starting_bankroll,
                cash=self.paper.cash,
                existing_market_exposure=exp.by_market.get(m.market_id, 0.0),
                existing_category_exposure=exp.by_category.get(m.category, 0.0),
                existing_total_exposure=exp.total,
                existing_group_exposure=exp.by_group.get(m.correlation_group, 0.0),
                duplicate=duplicate,
                halted=self.repo.state().halted,
                broker_ok=self.paper.operational(),
                data_fresh=True,
                canary=canary,
                open_positions=len(positions),
                **gemini_kw,
            )
            ident = self._market_identity(m)
            gates_out = decision.gates_dict()
            gates_out["ai_provider"] = {"passed": True, "detail": self._provider_tag()}
            gates_out["market"] = {"passed": True, "detail": f"{ident['instrument']} | {ident['question'][:80]}", **ident}
            did = self.repo.insert_decision(
                {
                    "market_id": m.market_id,
                    "token_id": decision.token_id,
                    "side": decision.side,
                    "approved": decision.approved,
                    "reject_reason": decision.reject_reason,
                    "gates": gates_out,
                    "grok_p": decision.grok_p,
                    "market_price": decision.market_price,
                    "raw_edge": decision.raw_edge,
                    "execution_adjusted_edge": decision.execution_adjusted_edge,
                    "kelly": decision.kelly,
                    "size_usd": decision.size_usd,
                    "size_shares": decision.size_shares,
                    "strategy_version": self.settings.strategy_version,
                    "prompt_version": self.settings.prompt_version,
                    "prediction_id": pred_id,
                    "idempotency_key": key if decision.approved else None,
                }
            )
            if not decision.approved:
                self.cycle_stats["rejected"] += 1
                self.log.info(
                    "REJECT instrument=%s reason=%s %s",
                    ident["instrument"],
                    decision.reject_reason,
                    ident["question"][:80],
                )
                self.repo.event(
                    "TRADE_REJECTED",
                    f"{ident['instrument']} | {decision.reject_reason or ''} | {ident['question'][:80]}",
                    ident,
                )
                continue
            block = self.risk.order_block_reason(
                size_usd=decision.size_usd,
                existing_total_exposure=exp.total,
                bankroll=self.settings.paper_starting_bankroll,
            )
            if block:
                self.cycle_stats["rejected"] += 1
                self.repo.event(
                    "TRADE_REJECTED",
                    f"{ident['instrument']} | {block} | {ident['question'][:80]}",
                    ident,
                )
                continue
            self.repo.event("OPPORTUNITY", m.question[:120], {"edge": decision.raw_edge})
            err = await self.executor.execute(decision, did)
            if err:
                self.repo.event("TRADE_REJECTED", err)
            positions = await self.paper.positions()
            break  # one token per market per cycle
        return True


    def _market_identity(self, m) -> dict:
        inst = _instrument_label(m)
        q = (m.question or "")[:120]
        return {
            "instrument": inst,
            "question": q,
            "category": m.category or "",
            "market_id": m.market_id,
        }

    def _reject(self, m, reason: str, extra: dict | None = None) -> None:
        self.cycle_stats["rejected"] += 1
        extra = dict(extra or {})
        detail = ""
        if reason == "spread_too_wide" and extra.get("spread") is not None:
            detail = f"spread={float(extra['spread']):.4f} max={float(extra.get('max_spread', self.settings.max_spread)):.4f}"
        elif reason == MID_OUTSIDE_BAND:
            mid = extra.get("mid")
            lo = extra.get("min_tradeable_mid", self.settings.min_tradeable_mid)
            hi = extra.get("max_tradeable_mid", self.settings.max_tradeable_mid)
            shown = "none" if mid is None else f"{float(mid):.4f}"
            detail = f"mid={shown} band=[{float(lo):.4f},{float(hi):.4f}]"
        ident = self._market_identity(m)
        gates = {
            reason: {"passed": False, "detail": detail},
            "ai_provider": {"passed": True, "detail": self._provider_tag()},
            "market": {"passed": True, "detail": f"{ident['instrument']} | {ident['question'][:80]}", **ident},
        }
        if reason == "spread_too_wide" and extra.get("spread") is not None:
            gates["spread_width"] = {
                "passed": False,
                "detail": detail,
                "spread": float(extra["spread"]),
                "max_spread": float(extra.get("max_spread", self.settings.max_spread)),
                "best_bid": extra.get("best_bid"),
                "best_ask": extra.get("best_ask"),
                "bucket": _spread_bucket(float(extra["spread"])),
            }
        payload = {**ident, **extra}
        if "spread" in extra:
            payload["spread_bucket"] = _spread_bucket(float(extra["spread"]))
        self.repo.insert_decision(
            {
                "market_id": m.market_id,
                "approved": False,
                "reject_reason": reason,
                "gates": gates,
                "strategy_version": self.settings.strategy_version,
                "prompt_version": self.settings.prompt_version,
            }
        )
        self.log.info(
            "REJECT instrument=%s reason=%s %s",
            ident["instrument"],
            reason,
            ident["question"][:80],
        )
        msg = f"{ident['instrument']} | {reason} | {ident['question'][:80]}"
        self.repo.event("TRADE_REJECTED", msg, payload)

    async def run_forever(self) -> None:
        await self.start_clock()
        text = banner_for(self.settings, self.repo)
        print(text)
        await self.notify(text)
        while not self.stop:
            try:
                await self.cycle()
            except DatabaseError as exc:
                self.risk.kill.trigger(f"database_failure:{exc}")
                await self.notify("KILL_SWITCH database_failure")
                break
            await asyncio.sleep(self.settings.loop_seconds)

    def extra_stats(self) -> dict:
        snap = {
            "equity": self.paper.equity(),
            "cash": self.paper.cash,
            "reserved": self.paper.reserved,
            "open_positions": len(self.paper._positions),
            "open_orders": len(self.paper.resting),
            "exposure": self.paper.exposure(),
            "daily_pnl": self.risk.daily_pnl,
            "cum_pnl": self.paper.equity() - self.settings.paper_starting_bankroll,
            "drawdown": 0.0,
            "candidates": self.cycle_stats["candidates"],
            "rejected": self.cycle_stats["rejected"],
            "avg_edge": self.cycle_stats["avg_edge"],
            "avg_exec_edge": self.cycle_stats["avg_exec_edge"],
            "avg_slip": self.cycle_stats["avg_slip"],
            "partials": self.cycle_stats["partials"],
            "roi": (self.paper.equity() / self.settings.paper_starting_bankroll) - 1,
            "largest_position": max((p.shares * p.avg_price for p in self.paper._positions.values()), default=0.0),
            "max_exposure": self.paper.exposure(),
        }
        return snap


async def async_run(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    app = TradingApp(settings)
    await app.run_forever()


def run() -> None:
    asyncio.run(async_run())
