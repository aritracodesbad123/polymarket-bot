from app.evaluation.calibration import brier, buckets, log_loss


def test_brier_perfect():
    assert brier(1.0, 1) == 0.0
    assert brier(0.0, 0) == 0.0


def test_buckets():
    rows = [(0.05, 0), (0.05, 0), (0.95, 1)]
    b = buckets(rows)
    assert b[0]["n"] == 2
    assert b[9]["n"] == 1
    assert log_loss(0.9, 1) > 0
