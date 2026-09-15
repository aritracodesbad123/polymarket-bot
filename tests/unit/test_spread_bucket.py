from app.main import _spread_bucket


def test_spread_buckets():
    assert _spread_bucket(0.05) == "le_6c"
    assert _spread_bucket(0.06) == "le_6c"
    assert _spread_bucket(0.07) == "7_10c"
    assert _spread_bucket(0.10) == "7_10c"
    assert _spread_bucket(0.11) == "11_15c"
    assert _spread_bucket(0.15) == "11_15c"
    assert _spread_bucket(0.16) == "gt_15c"
