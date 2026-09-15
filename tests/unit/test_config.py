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
    assert s.kelly_multiplier == 0.25
    assert s.live_trading_enabled is False
    assert s.trading_mode == "paper"
