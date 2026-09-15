from __future__ import annotations


class CreditHardFail(Exception):
    """xAI credit/auth hard-fail. Do not retry Grok; may fall through to Gemini."""


def is_credit_hard_fail(exc: BaseException) -> bool:
    t = f"{type(exc).__name__} {exc}".lower()
    return (
        "permission_denied" in t
        or "spending limit" in t
        or "used all available credits" in t
        or "monthly spending limit" in t
    )
