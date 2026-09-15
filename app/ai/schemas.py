from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MarketEstimate(BaseModel):
    market_id: str
    estimated_probability: float
    confidence: Literal["low", "medium", "high"]
    confidence_score: float
    base_rate_probability: float
    evidence_adjustment: float
    key_evidence: list[str] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list)
    uncertainty_factors: list[str] = Field(default_factory=list)
    stale_information_risk: Literal["low", "medium", "high"] = "medium"
    should_abstain: bool
    abstention_reason: str = ""
    reasoning_summary: str

    @field_validator(
        "estimated_probability",
        "confidence_score",
        "base_rate_probability",
    )
    @classmethod
    def _unit(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError("probability/score must be in [0, 1]")
        return v
