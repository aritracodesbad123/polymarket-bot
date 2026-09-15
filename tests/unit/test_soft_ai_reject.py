from app.main import _soft_ai_reject


def test_soft_gemini_cooldown():
    assert _soft_ai_reject("gemini_error:gemini_cooldown")


def test_soft_cascade_exhausted():
    assert _soft_ai_reject("gemini_error:gemini_cascade_exhausted:ValueError")


def test_hard_probability_out_of_range():
    assert not _soft_ai_reject("probability_out_of_range")


def test_hard_unexpected():
    assert not _soft_ai_reject("grok_error:boom")
