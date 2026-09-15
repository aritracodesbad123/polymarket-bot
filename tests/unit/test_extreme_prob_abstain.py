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


def test_extreme_high_abstains():
    out = _eng()._finish(_est(0.998), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is True
    assert "extreme_probability" in out.abstention_reason


def test_extreme_low_abstains():
    out = _eng()._finish(_est(0.01), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is True


def test_mid_range_ok():
    out = _eng()._finish(_est(0.55), EvidencePacket(market_id="m1", question="q", resolution_criteria="r"))
    assert out.should_abstain is False
