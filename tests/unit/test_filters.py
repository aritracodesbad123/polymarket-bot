from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.market_data.scanner import filter_book, filter_market
from tests.conftest import book, market


def test_reject_closed():
    s = Settings()
    assert filter_market(market(closed=True, status="closed", active=False), s) == "closed"


def test_reject_near_resolution():
    s = Settings(min_time_to_resolution_hours=6)
    m = market(close_time=datetime.now(timezone.utc) + timedelta(hours=1))
    assert filter_market(m, s) == "too_close_to_resolution"


def test_stale_book():
    s = Settings(max_data_age_seconds=15)
    assert filter_book(book(age_s=30), s) == "stale_data"


def test_wide_spread():
    s = Settings(max_spread=0.02)
    assert filter_book(book(ask=0.6, bid=0.4), s) == "spread_too_wide"
