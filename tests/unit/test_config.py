import os

from app.config import Settings


def test_paper_settings_have_no_private_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("PRIVATE_KEY", "0xdead")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xdead")
    s = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert "private_key" not in s.model_dump()
    assert s.trading_mode == "paper"


def test_defaults():
    s = Settings()
    assert s.min_edge == 0.05
    assert s.max_spread == 0.06
    assert s.kelly_multiplier == 0.25
    assert s.live_trading_enabled is False
    assert s.trading_mode == "paper"
    assert s.api_die_cushion_usd == 0.50
    assert s.ai_session_budget_usd == 10.0
    assert s.estimated_usd_per_ai_call == 0.02
    assert s.kill_floor_pct == 0.20
    assert s.weekly_loss_pct is None
    assert s.max_position_usd is None
    assert s.max_total_exposure_usd is None
    assert s.max_daily_loss_usd is None


def test_gemini_key_stripped(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
    s = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert s.gemini_api_key == "secret-gemini"
    assert "secret-gemini" not in str(s.public_dict())
