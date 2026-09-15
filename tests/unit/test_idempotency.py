from app.execution.executor import idempotency_key


def test_same_window_same_key():
    a = idempotency_key("m", "t", "BUY", "1", ts=1000)
    b = idempotency_key("m", "t", "BUY", "1", ts=1000)
    assert a == b


def test_different_token_different_key():
    a = idempotency_key("m", "t1", "BUY", "1", ts=1000)
    b = idempotency_key("m", "t2", "BUY", "1", ts=1000)
    assert a != b
