from app.strategy.evaluator import StrategyEvaluator
from tests.conftest import book, estimate, market, settings


def _eval(tmp_path, **kw):
    s = settings(tmp_path)
    m = market()
    b = book()
    est = estimate()
    args = dict(
        market=m,
        book=b,
        estimate=est,
        packet=object(),
        bankroll=1000,
        cash=1000,
        existing_market_exposure=0,
        existing_category_exposure=0,
        existing_total_exposure=0,
        existing_group_exposure=0,
        duplicate=False,
        halted=False,
        broker_ok=True,
        data_fresh=True,
    )
    args.update(kw)
    return StrategyEvaluator(s).evaluate(**args)


def test_edge_too_small(tmp_path):
    d = _eval(tmp_path, estimate=estimate(0.41))  # ask 0.40, edge 0.01 < 0.05
    assert not d.approved
    assert d.reject_reason == "edge_too_small"


def test_approved_with_edge(tmp_path):
    d = _eval(tmp_path, estimate=estimate(0.70))
    assert d.approved
    assert d.raw_edge >= 0.05
    assert d.size_usd > 0


def test_duplicate_rejected(tmp_path):
    d = _eval(tmp_path, duplicate=True)
    assert not d.approved
    assert d.reject_reason == "duplicate_order"


def test_halted(tmp_path):
    d = _eval(tmp_path, halted=True)
    assert d.reject_reason == "halted"


def test_stale(tmp_path):
    d = _eval(tmp_path, data_fresh=False)
    assert d.reject_reason == "stale_data"


def test_abstain(tmp_path):
    d = _eval(tmp_path, estimate=estimate(0.7, abstain=True))
    assert d.reject_reason == "grok_abstain"


def test_risk_limit(tmp_path):
    d = _eval(tmp_path, existing_total_exposure=1000)
    assert not d.approved


def test_correlation_limit(tmp_path):
    d = _eval(tmp_path, existing_group_exposure=1000)
    assert not d.approved
