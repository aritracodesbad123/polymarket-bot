"""Read-only discovery mapping (no orders)."""

from app.market_data.client import market_from_sdk


def test_map_gamma_row():
    m = market_from_sdk(
        {
            "id": "123",
            "question": "Will it rain?",
            "description": "NOAA.",
            "clobTokenIds": '["yesTok","noTok"]',
            "closed": False,
            "active": True,
            "volume24hr": 5000,
            "liquidity": 2000,
            "endDate": "2026-12-01T00:00:00Z",
            "negRisk": False,
        }
    )
    assert m is not None
    assert m.yes_token_id == "yesTok"
    assert m.no_token_id == "noTok"
    assert m.question == "Will it rain?"
    assert not m.closed
