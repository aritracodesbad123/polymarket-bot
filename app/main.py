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
from app.market_data.scanner import MarketScanner, filter_book, filter_market
from app.monitoring.logging import setup_logging
from app.monitoring.telegram import Telegram
from app.portfolio.portfolio import Portfolio
from app.research.researcher import EvidencePacket, NullResearchProvider, XAISearchProvider
from app.risk.manager import RiskManager, exposure_from_positions
from app.storage.db import Database, DatabaseError
from app.storage.repositories import Repositories
from app.strategy.evaluator import StrategyEvaluator

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

GEMINI_EDGE_TIGHTEN = 0.02


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
        self.risk = RiskManager(settings, self.repo)
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
        st = self.repo.state()
        if st.halted:
            return
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
        marks: dict[str, float] = {}
        positions = await self.paper.positions()
        for m in candidates:
            if grok_calls >= self.settings.max_grok_calls_per_cycle:
                break
            if self.repo.state().halted:
                break
            used = await self._consider(m, positions, marks)
            if used:
                grok_calls += 1
            gemini_ready = bool(self.engine and self.engine.gemini)
            grok_dead = bool(
                getattr(self.research, "blocked", False)
                or (self.engine and self.engine.grok and self.engine.grok.blocked)
            )
            if grok_dead and not gemini_ready:
                break
        self.portfolio.snapshot(marks)
        mismatch = await self.paper.reconcile()
        if mismatch:
            self.risk.note_recon_mismatch(mismatch)

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
            self._reject(m, book_reject)
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
            self.cycle_stats["ai_errors"] += 1
            self.risk.note_ai_error(self.cycle_stats["ai_errors"])
            self.repo.event("GROK_ERROR", exc.reason)
            self._reject(m, exc.reason)
            return True
        if self._pending_ai_provider_notice:
            await self.notify(self._pending_ai_provider_notice)
            self._pending_ai_provider_notice = None
        pred_id = self.repo.insert_prediction(
            {
                "market_id": m.market_id,
                "prompt_version": self.settings.prompt_version,
                "model": self.engine.last_model or self.settings.grok_model,
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
        edge_floor = self.settings.min_edge
        if self.engine.provider == "gemini":
            edge_floor += GEMINI_EDGE_TIGHTEN
        for book in (yes_book, no_book):
            if book is None:
                continue
            reason = filter_book(book, self.settings)
            if reason:
                if reason == "stale_data":
                    self.risk.note_stale()
                self._reject(m, reason)
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
                min_edge=edge_floor,
            )
            did = self.repo.insert_decision(
                {
                    "market_id": m.market_id,
                    "token_id": decision.token_id,
                    "side": decision.side,
                    "approved": decision.approved,
                    "reject_reason": decision.reject_reason,
                    "gates": decision.gates_dict(),
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
                self.repo.event("TRADE_REJECTED", decision.reject_reason or "", {"market_id": m.market_id})
                continue
            self.repo.event("OPPORTUNITY", m.question[:120], {"edge": decision.raw_edge})
            err = await self.executor.execute(decision, did)
            if err:
                self.repo.event("TRADE_REJECTED", err)
            positions = await self.paper.positions()
            break  # one token per market per cycle
        return True

    def _reject(self, m, reason: str) -> None:
        self.cycle_stats["rejected"] += 1
        self.repo.insert_decision(
            {
                "market_id": m.market_id,
                "approved": False,
                "reject_reason": reason,
                "gates": {reason: {"passed": False, "detail": ""}},
                "strategy_version": self.settings.strategy_version,
                "prompt_version": self.settings.prompt_version,
            }
        )
        self.repo.event("TRADE_REJECTED", reason, {"market_id": m.market_id})

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
