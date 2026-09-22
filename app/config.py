"""Environment → Settings. Paper mode never reads PRIVATE_KEY."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

DIR = Path(__file__).resolve().parent.parent

TradingMode = Literal["paper", "live"]


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _opt_f(name: str) -> float | None:
    """Unset or blank → None (feature off). Explicit 0 is a real value."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return float(raw.strip())


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _s(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw.strip()


class Settings(BaseModel):
    trading_mode: TradingMode = "paper"
    live_trading_enabled: bool = False

    xai_api_key: str | None = None
    grok_model: str = "grok-4.6"
    gemini_api_key: str | None = None

    polymarket_api_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    paper_starting_bankroll: float = 1000.0
    paper_latency_ms: int = 150
    db_path: str = "polygrok.db"

    strategy_version: str = "1"
    prompt_version: str = "probability_v1"
    min_edge: float = 0.05
    kelly_multiplier: float = 0.25

    max_position_pct_bankroll: float = 0.03
    max_market_exposure_pct: float = 0.05
    max_total_exposure_pct: float = 0.25
    max_category_exposure_pct: float = 0.10
    max_correlation_group_exposure_pct: float = 0.10
    max_slippage_pct: float = 0.02
    max_daily_loss_pct: float = 0.05
    # Optional absolute USD caps. None preserves percentage-only behavior.
    max_position_usd: float | None = None
    max_total_exposure_usd: float | None = None
    max_daily_loss_usd: float | None = None
    max_consecutive_losses: int = 5
    min_liquidity_multiple: float = 3.0

    min_liquidity: float = 500.0
    min_volume: float = 2000.0
    max_spread: float = 0.06
    min_time_to_resolution_hours: float = 6.0
    max_time_to_resolution_hours: float = 720.0
    max_markets_per_cycle: int = 50
    max_grok_calls_per_cycle: int = 8
    max_data_age_seconds: float = 15.0
    loop_seconds: float = 30.0
    min_confidence_score: float = 0.4

    canary_max_order_usd: float = 5.0
    canary_max_daily_notional_usd: float = 20.0
    canary_max_open_positions: int = 3

    # Pay-for-yourself adaptive regime
    regime_enabled: bool = True
    holding_stop_pct: float = 0.25
    holding_thesis_edge: float = 0.02
    holding_max_hours: float = 48.0
    estimated_usd_per_ai_call: float = 0.02
    api_die_cushion_usd: float = 0.50
    defend_edge_tighten: float = 0.02
    defend_kelly_mult: float = 0.5
    defend_max_grok_calls: int = 3
    kill_floor_pct: float = 0.20  # DIE if equity <= start * (1 - this). Not the weekly stop.
    # Weekly equity stop vs the persisted week-start baseline. None/<=0 = off.
    # Distinct from kill_floor_pct (catastrophic, vs starting bankroll) and max_daily_loss_pct.
    weekly_loss_pct: float | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    market_blacklist: tuple[str, ...] = Field(default_factory=tuple)
    category_blacklist: tuple[str, ...] = Field(default_factory=tuple)
    # Comma-separated Gamma event tag_slugs (e.g. "crypto,forex"). Empty = all markets.
    universe_tags: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def from_env(cls, *, dotenv_path: Path | None = None) -> Settings:
        load_dotenv(dotenv_path or (DIR / ".env"), override=False)
        mode_raw = _s("TRADING_MODE", "paper").lower()
        trading_mode: TradingMode = "live" if mode_raw == "live" else "paper"
        blacklist = tuple(
            x.strip() for x in _s("MARKET_BLACKLIST", "").split(",") if x.strip()
        )
        cat_bl = tuple(
            x.strip() for x in _s("CATEGORY_BLACKLIST", "").split(",") if x.strip()
        )
        xai = os.environ.get("XAI_API_KEY") or None
        gemini = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or None
        return cls(
            trading_mode=trading_mode,
            live_trading_enabled=_b("LIVE_TRADING_ENABLED", False),
            xai_api_key=xai,
            gemini_api_key=gemini,
            grok_model=_s("GROK_MODEL", "grok-4.6"),
            polymarket_api_url=_s("POLYMARKET_API_URL", "https://clob.polymarket.com"),
            polymarket_gamma_url=_s(
                "POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"
            ),
            polymarket_ws_url=_s(
                "POLYMARKET_WS_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),
            paper_starting_bankroll=_f("PAPER_STARTING_BANKROLL", 1000.0),
            paper_latency_ms=_i("PAPER_LATENCY_MS", 150),
            db_path=_s("DB_PATH", "polygrok.db"),
            strategy_version=_s("STRATEGY_VERSION", "1"),
            prompt_version=_s("PROMPT_VERSION", "probability_v1"),
            min_edge=_f("MIN_EDGE", 0.05),
            kelly_multiplier=_f("KELLY_MULTIPLIER", 0.25),
            max_position_pct_bankroll=_f("MAX_POSITION_PCT_BANKROLL", 0.03),
            max_market_exposure_pct=_f("MAX_MARKET_EXPOSURE_PCT", 0.05),
            max_total_exposure_pct=_f("MAX_TOTAL_EXPOSURE_PCT", 0.25),
            max_category_exposure_pct=_f("MAX_CATEGORY_EXPOSURE_PCT", 0.10),
            max_correlation_group_exposure_pct=_f(
                "MAX_CORRELATION_GROUP_EXPOSURE_PCT", 0.10
            ),
            max_slippage_pct=_f("MAX_SLIPPAGE_PCT", 0.02),
            max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 0.05),
            max_position_usd=_opt_f("MAX_POSITION_USD"),
            max_total_exposure_usd=_opt_f("MAX_TOTAL_EXPOSURE_USD"),
            max_daily_loss_usd=_opt_f("MAX_DAILY_LOSS_USD"),
            max_consecutive_losses=_i("MAX_CONSECUTIVE_LOSSES", 5),
            min_liquidity_multiple=_f("MIN_LIQUIDITY_MULTIPLE", 3.0),
            min_liquidity=_f("MIN_LIQUIDITY", 500.0),
            min_volume=_f("MIN_VOLUME", 2000.0),
            max_spread=_f("MAX_SPREAD", 0.06),
            min_time_to_resolution_hours=_f("MIN_TIME_TO_RESOLUTION_HOURS", 6.0),
            max_time_to_resolution_hours=_f("MAX_TIME_TO_RESOLUTION_HOURS", 720.0),
            max_markets_per_cycle=_i("MAX_MARKETS_PER_CYCLE", 50),
            max_grok_calls_per_cycle=_i("MAX_GROK_CALLS_PER_CYCLE", 8),
            max_data_age_seconds=_f("MAX_DATA_AGE_SECONDS", 15.0),
            loop_seconds=_f("LOOP_SECONDS", 30.0),
            min_confidence_score=_f("MIN_CONFIDENCE_SCORE", 0.4),
            canary_max_order_usd=_f("CANARY_MAX_ORDER_USD", 5.0),
            canary_max_daily_notional_usd=_f("CANARY_MAX_DAILY_NOTIONAL_USD", 20.0),
            canary_max_open_positions=_i("CANARY_MAX_OPEN_POSITIONS", 3),
            regime_enabled=_b("REGIME_ENABLED", True),
            holding_stop_pct=_f("HOLDING_STOP_PCT", 0.25),
            holding_thesis_edge=_f("HOLDING_THESIS_EDGE", 0.02),
            holding_max_hours=_f("HOLDING_MAX_HOURS", 48.0),
            estimated_usd_per_ai_call=_f("ESTIMATED_USD_PER_AI_CALL", 0.02),
            api_die_cushion_usd=_f("API_DIE_CUSHION_USD", 0.50),
            defend_edge_tighten=_f("DEFEND_EDGE_TIGHTEN", 0.02),
            defend_kelly_mult=_f("DEFEND_KELLY_MULT", 0.5),
            defend_max_grok_calls=_i("DEFEND_MAX_GROK_CALLS", 3),
            kill_floor_pct=_f("KILL_FLOOR_PCT", 0.20),
            weekly_loss_pct=_opt_f("WEEKLY_LOSS_PCT"),
            telegram_bot_token=tg_token,
            telegram_chat_id=tg_chat,
            market_blacklist=blacklist,
            category_blacklist=cat_bl,
            universe_tags=tuple(
                x.strip().lower()
                for x in (
                    _s("UNIVERSE_TAGS", "") or _s("UNIVERSE_TAG", "")
                ).split(",")
                if x.strip()
            ),
        )

    def public_dict(self) -> dict:
        d = self.model_dump()
        d.pop("xai_api_key", None)
        d.pop("gemini_api_key", None)
        d.pop("telegram_bot_token", None)
        return d

    def config_hash(self) -> str:
        payload = json.dumps(self.public_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


def live_credentials_present() -> bool:
    """True if env has a key + wallet. Does not load them into Settings."""
    key = os.environ.get("PRIVATE_KEY") or os.environ.get("POLYMARKET_PRIVATE_KEY")
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    return bool(key and wallet)


def load_live_credentials() -> tuple[str, str]:
    """Return (private_key, wallet). Caller must already have live authorization."""
    key = os.environ.get("PRIVATE_KEY") or os.environ.get("POLYMARKET_PRIVATE_KEY")
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if not key or not wallet:
        raise PermissionError("live credentials missing")
    return key, wallet
