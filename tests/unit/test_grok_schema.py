import pytest

from app.ai.schemas import MarketEstimate
from pydantic import ValidationError


def test_probability_1_2_rejected():
    with pytest.raises(ValidationError):
        MarketEstimate(
            market_id="m",
            estimated_probability=1.2,
            confidence="low",
            confidence_score=0.1,
            base_rate_probability=0.5,
            evidence_adjustment=0.0,
            should_abstain=False,
            reasoning_summary="x",
        )


def test_malformed_missing_fields():
    with pytest.raises(ValidationError):
        MarketEstimate.model_validate({"estimated_probability": 0.4})
