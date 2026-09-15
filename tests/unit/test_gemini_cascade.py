from app.ai.gemini_client import (
    GEMINI_CASCADE,
    gemini_cascade,
    normalize_estimate_obj,
    parse_estimate_json,
)


def test_cascade_prefers_25_flash():
    assert gemini_cascade()[0] == "gemini-2.5-flash"
    assert "gemini-2.5-flash" in GEMINI_CASCADE


def test_normalize_none_abstention_and_string_lists():
    obj = normalize_estimate_obj(
        {
            "market_id": "m1",
            "estimated_probability": 0.55,
            "confidence": "Low",
            "confidence_score": 2,
            "base_rate_probability": 0.5,
            "evidence_adjustment": 0.05,
            "key_evidence": "one fact",
            "counterarguments": "one counter",
            "uncertainty_factors": "noise",
            "stale_information_risk": "Medium",
            "should_abstain": False,
            "abstention_reason": None,
            "reasoning_summary": "thin",
        }
    )
    est = parse_estimate_json(__import__("json").dumps(obj))
    assert est.confidence == "low"
    assert 0.0 <= est.confidence_score <= 1.0
    assert est.key_evidence == ["one fact"]
    assert est.abstention_reason == ""
    assert est.stale_information_risk == "medium"


def test_parse_probability_alias():
    raw = '{"market_id":"m1","probability":0.6,"confidence":"medium","confidence_score":0.7,"base_rate_probability":0.5,"evidence_adjustment":0.1,"key_evidence":[],"counterarguments":[],"uncertainty_factors":[],"stale_information_risk":"low","should_abstain":false,"abstention_reason":"","reasoning_summary":"ok"}'
    est = parse_estimate_json(raw)
    assert est.estimated_probability == 0.6
