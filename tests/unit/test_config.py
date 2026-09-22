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
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90
    assert s.estimator is None
    assert s.micro_lambda == 0.08
    assert s.micro_min_abs_imbalance == 0.40
    assert s.micro_coin_flip_min == 0.45
    assert s.micro_coin_flip_max == 0.55
    assert s.weekly_loss_pct is None
    assert s.max_position_usd is None
    assert s.max_total_exposure_usd is None
    assert s.max_daily_loss_usd is None


def test_tradeable_mid_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MIN_TRADEABLE_MID", raising=False)
    monkeypatch.delenv("MAX_TRADEABLE_MID", raising=False)
    s = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert s.min_tradeable_mid == 0.10
    assert s.max_tradeable_mid == 0.90

    monkeypatch.setenv("MIN_TRADEABLE_MID", "")
    monkeypatch.setenv("MAX_TRADEABLE_MID", "")
    blank = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert blank.min_tradeable_mid == 0.10
    assert blank.max_tradeable_mid == 0.90

    monkeypatch.setenv("MIN_TRADEABLE_MID", "0.15")
    monkeypatch.setenv("MAX_TRADEABLE_MID", "0.85")
    overridden = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert overridden.min_tradeable_mid == 0.15
    assert overridden.max_tradeable_mid == 0.85


def test_estimator_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ESTIMATOR", raising=False)
    monkeypatch.delenv("ESTIMATOR_AUTO_SWITCH", raising=False)
    blank = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert blank.estimator is None
    assert blank.estimator_auto_switch is True

    monkeypatch.setenv("ESTIMATOR", "")
    empty = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert empty.estimator is None

    monkeypatch.setenv("ESTIMATOR", "microstructure")
    armed = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert armed.estimator == "microstructure"
    assert armed.estimator_auto_switch is True

    monkeypatch.setenv("ESTIMATOR", "OFF")
    off = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert off.estimator == "off"

    monkeypatch.setenv("ESTIMATOR", "microstructure")
    monkeypatch.setenv("ESTIMATOR_AUTO_SWITCH", "0")
    disabled = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert disabled.estimator == "microstructure"
    assert disabled.estimator_auto_switch is False


def test_micro_phase1_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MICRO_LAMBDA", raising=False)
    monkeypatch.delenv("MICRO_MIN_ABS_I", raising=False)
    monkeypatch.delenv("MICRO_COIN_FLIP_MIN", raising=False)
    monkeypatch.delenv("MICRO_COIN_FLIP_MAX", raising=False)
    blank = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert blank.micro_lambda == 0.08
    assert blank.micro_min_abs_imbalance == 0.40
    assert blank.micro_coin_flip_min == 0.45
    assert blank.micro_coin_flip_max == 0.55

    monkeypatch.setenv("MICRO_LAMBDA", "0.12")
    monkeypatch.setenv("MICRO_MIN_ABS_I", "0.55")
    monkeypatch.setenv("MICRO_COIN_FLIP_MIN", "0.48")
    monkeypatch.setenv("MICRO_COIN_FLIP_MAX", "0.52")
    overridden = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert overridden.micro_lambda == 0.12
    assert overridden.micro_min_abs_imbalance == 0.55
    assert overridden.micro_coin_flip_min == 0.48
    assert overridden.micro_coin_flip_max == 0.52


def test_gemini_key_stripped(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
    s = Settings.from_env(dotenv_path=tmp_path / "none.env")
    assert s.gemini_api_key == "secret-gemini"
    assert "secret-gemini" not in str(s.public_dict())
