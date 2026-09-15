from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.market_data.models import Market
from app.market_data.scanner import filter_market


def _m(question: str, category: str = "other") -> Market:
    return Market(
        market_id="m1",
        question=question,
        description="d",
        resolution_criteria="r",
        yes_token_id="y",
        no_token_id="n",
        category=category,
        close_time=datetime.now(timezone.utc) + timedelta(days=10),
        liquidity=5000,
        volume=5000,
        active=True,
    )


def test_crypto_universe_rejects_politics():
    s = Settings(universe_tag="crypto")
    assert filter_market(_m("Will X win the election?", "politics"), s) == "outside_universe"


def test_crypto_universe_allows_bitcoin():
    s = Settings(universe_tag="crypto")
    assert filter_market(_m("Will Bitcoin reach 100k?", "crypto"), s) is None
