import pytest

from app.strategy.evaluator import (
    expected_profit_per_share,
    expected_return_on_cost,
    fee_per_share,
    full_kelly,
    quarter_kelly,
)


def test_ev_yes():
    assert expected_profit_per_share(0.7, 0.4) == pytest.approx(0.3)
    assert expected_return_on_cost(0.7, 0.4) == pytest.approx(0.3 / 0.4)


def test_full_kelly_formula():
    p, c = 0.7, 0.4
    b = (1 - c) / c
    q = 1 - p
    assert abs(full_kelly(p, c) - ((b * p) - q) / b) < 1e-12


def test_quarter_kelly():
    assert abs(quarter_kelly(0.7, 0.4, 0.25) - 0.25 * full_kelly(0.7, 0.4)) < 1e-12


def test_negative_kelly_clamped():
    assert full_kelly(0.3, 0.4) == 0.0
    assert quarter_kelly(0.3, 0.4) == 0.0


def test_no_probability():
    p_yes = 0.7
    assert abs((1 - p_yes) - 0.3) < 1e-12


def test_fee_geo_zero():
    assert fee_per_share(0.5, "geopolitics") == 0.0


def test_fee_crypto_peak():
    f = fee_per_share(0.5, "crypto")
    assert abs(f - 0.07 * 0.5 * 0.5) < 1e-12
