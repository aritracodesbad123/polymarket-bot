import pytest

from app.ai.credits import CreditHardFail, is_credit_hard_fail
from app.ai.grok_client import GrokClient
from app.ai.probability_engine import InvalidEstimate, ProbabilityEngine
from app.ai.prompt_manager import PromptManager
from app.research.researcher import EvidencePacket, XAISearchProvider
from tests.conftest import estimate


def test_classifies_permission_denied():
    assert is_credit_hard_fail(
        RuntimeError(
            "PERMISSION_DENIED: Your team has either used all available credits "
            "or reached its monthly spending limit"
        )
    )
    assert not is_credit_hard_fail(RuntimeError("timeout"))


@pytest.mark.asyncio
async def test_blocked_research_does_not_call_xai():
    p = XAISearchProvider("k")
    p.blocked = True
    pkt = EvidencePacket(market_id="m1", question="q", resolution_criteria="r")
    with pytest.raises(CreditHardFail):
        await p.gather(pkt)


@pytest.mark.asyncio
async def test_blocked_grok_does_not_construct_client():
    c = GrokClient("k", "grok-4.6")
    c.blocked = True
    with pytest.raises(CreditHardFail):
        await c.estimate("sys", "user")


@pytest.mark.asyncio
async def test_engine_flips_to_gemini_on_credit_fail():
    class FakeGrok:
        blocked = False
        model = "grok-4.6"

        async def estimate(self, *_a):
            raise CreditHardFail("PERMISSION_DENIED spending limit")

    class FakeGemini:
        last_model = "gemini-3.1-pro"

        async def estimate(self, *_a):
            return estimate()

    swapped = []
    eng = ProbabilityEngine(
        FakeGrok(),  # type: ignore[arg-type]
        PromptManager("probability_v1"),
        FakeGemini(),  # type: ignore[arg-type]
        on_provider_swap=lambda m: swapped.append(m),
    )
    pkt = EvidencePacket(market_id="m1", question="q", resolution_criteria="r")
    est = await eng.estimate(pkt)
    assert eng.provider == "gemini"
    assert est.estimated_probability == 0.7
    assert swapped == ["gemini-3.1-pro"]


@pytest.mark.asyncio
async def test_engine_abstains_without_gemini():
    class FakeGrok:
        blocked = False
        model = "grok-4.6"

        async def estimate(self, *_a):
            raise CreditHardFail("PERMISSION_DENIED")

    eng = ProbabilityEngine(FakeGrok(), PromptManager("probability_v1"), None)  # type: ignore[arg-type]
    pkt = EvidencePacket(market_id="m1", question="q", resolution_criteria="r")
    with pytest.raises(InvalidEstimate) as ei:
        await eng.estimate(pkt)
    assert ei.value.reason == "xai_credits_exhausted"


@pytest.mark.asyncio
async def test_engine_gemini_cascade_exhausted_abstains():
    class FakeGemini:
        last_model = "gemini-3.1-pro"

        async def estimate(self, *_a):
            raise RuntimeError("gemini_cascade_exhausted:HTTP 404")

    eng = ProbabilityEngine(
        None,
        PromptManager("probability_v1"),
        FakeGemini(),  # type: ignore[arg-type]
    )
    pkt = EvidencePacket(market_id="m1", question="q", resolution_criteria="r")
    with pytest.raises(InvalidEstimate) as ei:
        await eng.estimate(pkt)
    assert "gemini_error" in ei.value.reason
    assert "gemini_cascade_exhausted" in ei.value.reason
