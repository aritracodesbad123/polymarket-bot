from app.ai.probability_engine import ProbabilityEngine
from app.ai.prompt_manager import PromptManager
from app.ai.schemas import MarketEstimate
from app.research.researcher import EvidencePacket


def _eng() -> ProbabilityEngine:
    return ProbabilityEngine(None, PromptManager("probability_v1"), None)


def _est(p: float) -> MarketEstimate:
    return MarketEstimate(
        market_id="m1",
        estimated_probability=p,
        confidence="high",
        confidence_score=0.9,
        base_rate_probability=0.5,
        evidence_adjustment=0.0,
        should_abstain=False,
        reasoning_summary="t",
    )


def test_exact_one_abstains():
    out = _eng()._finish(_est(1.0), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is True
    assert "extreme_probability" in out.abstention_reason


def test_exact_zero_abstains():
    out = _eng()._finish(_est(0.0), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is True


def test_near_extreme_does_not_auto_abstain():
    # Soft extremes used to force grok_abstain and burned the Gemini paper run.
    out = _eng()._finish(_est(0.01), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is False


def test_mid_range_ok():
    out = _eng()._finish(_est(0.55), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is False


def test_empty_evidence_prompt_does_not_invite_abstain():
    pkt = EvidencePacket(
        market_id="m1",
        question="Will X happen?",
        resolution_criteria="Official source.",
        implied_probability=0.42,
    )
    block = pkt.to_prompt_block()
    assert "No live search packet" in block
    assert "Do not abstain solely" in block
