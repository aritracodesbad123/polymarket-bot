from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.ai.schemas import MarketEstimate

log = logging.getLogger(__name__)

VERTEX = "https://aiplatform.googleapis.com/v1/publishers/google/models"  # Vertex only; not generativelanguage.googleapis.com
COOLDOWN_SECONDS = 90.0

# Prefer models that actually resolve on Vertex with this API key.
# Skip fantasy 3.1-pro ids that 404 and burn the cascade into cooldown.
# Vertex publisher API (x-goog-api-key) — NOT AI Studio / generativelanguage.
# Prefer gemini-3.6-flash first (confirmed working on this Vertex key).
GEMINI_CASCADE = (
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
)

SCHEMA_HINT = """Return ONLY one JSON object (no markdown) with exactly these keys:
market_id (string),
estimated_probability (number in [0,1]),
confidence (one of: low, medium, high — lowercase),
confidence_score (number in [0,1]),
base_rate_probability (number in [0,1]),
evidence_adjustment (number),
key_evidence (array of strings),
counterarguments (array of strings),
uncertainty_factors (array of strings),
stale_information_risk (one of: low, medium, high — lowercase),
should_abstain (boolean),
abstention_reason (string, use "" if none),
reasoning_summary (string).
"""


def gemini_cascade() -> tuple[str, ...]:
    return GEMINI_CASCADE


PostFn = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, str]]]


def _candidate_text(data: dict) -> str:
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    return "".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    )


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    return [str(v)]


def _unit01(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    # Some models return 1-5 or 0-100 confidence scales.
    if x > 1.0 and x <= 5.0:
        x = x / 5.0
    elif x > 1.0 and x <= 100.0:
        x = x / 100.0
    return min(1.0, max(0.0, x))


def _low_med_high(v: Any, default: str = "medium") -> str:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("low", "medium", "high"):
        return s
    if s.startswith("low"):
        return "low"
    if s.startswith("med"):
        return "medium"
    if s.startswith("high"):
        return "high"
    return default


def normalize_estimate_obj(obj: dict[str, Any]) -> dict[str, Any]:
    """Coerce messy Gemini JSON into MarketEstimate-friendly shapes."""
    out = dict(obj)
    # aliases
    if "estimated_probability" not in out:
        for k in ("probability", "p_yes", "yes_probability", "p"):
            if k in out:
                out["estimated_probability"] = out[k]
                break
    out["estimated_probability"] = _unit01(out.get("estimated_probability"), 0.5)
    out["confidence_score"] = _unit01(out.get("confidence_score"), 0.0)
    out["base_rate_probability"] = _unit01(out.get("base_rate_probability"), out["estimated_probability"])
    try:
        out["evidence_adjustment"] = float(out.get("evidence_adjustment") or 0.0)
    except (TypeError, ValueError):
        out["evidence_adjustment"] = 0.0
    out["confidence"] = _low_med_high(out.get("confidence"), "low")
    out["stale_information_risk"] = _low_med_high(out.get("stale_information_risk"), "medium")
    out["key_evidence"] = _as_list(out.get("key_evidence"))
    out["counterarguments"] = _as_list(out.get("counterarguments"))
    out["uncertainty_factors"] = _as_list(out.get("uncertainty_factors"))
    ar = out.get("abstention_reason")
    out["abstention_reason"] = "" if ar is None else str(ar)
    if "should_abstain" not in out or out.get("should_abstain") is None:
        out["should_abstain"] = False
    if not isinstance(out.get("should_abstain"), bool):
        out["should_abstain"] = str(out.get("should_abstain")).strip().lower() in (
            "1",
            "true",
            "yes",
        )
    out["market_id"] = str(out.get("market_id") or "")
    out["reasoning_summary"] = str(out.get("reasoning_summary") or "")
    return out


def parse_estimate_json(raw: str) -> MarketEstimate:
    text = raw.strip()
    fence = text.find("```")
    if fence >= 0:
        chunk = text[fence + 3 :]
        if chunk.lstrip().lower().startswith("json"):
            chunk = chunk.lstrip()[4:]
        end_fence = chunk.find("```")
        if end_fence >= 0:
            chunk = chunk[:end_fence]
        text = chunk
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("json not an object")
    return MarketEstimate.model_validate(normalize_estimate_obj(obj))


class GeminiClient:
    def __init__(self, api_key: str, post: PostFn | None = None) -> None:
        self.api_key = api_key
        self.last_model = GEMINI_CASCADE[0]
        self._cool_until = 0.0
        self._post = post

    async def estimate(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
        if time.time() < self._cool_until:
            raise RuntimeError("gemini_cooldown")
        system = f"{system_prompt.rstrip()}\n\n{SCHEMA_HINT}"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }
        headers = {"content-type": "application/json", "x-goog-api-key": self.api_key}
        last_exc: Exception | None = None
        saw_success_http = False
        for model in gemini_cascade():
            url = f"{VERTEX}/{model}:generateContent"
            log.warning("gemini attempt model=%s", model)
            try:
                status, body = await self._do_post(url, headers, payload)
            except Exception as exc:
                last_exc = exc
                log.warning("gemini %s failed: %s — next", model, exc)
                continue
            if status in (401, 403):
                last_exc = RuntimeError(f"gemini HTTP {status}")
                log.warning("gemini %s HTTP %s — abort cascade", model, status)
                break
            if status == 404:
                last_exc = RuntimeError(f"gemini HTTP 404")
                log.warning("gemini %s HTTP 404 — next", model)
                continue
            if status >= 400:
                last_exc = RuntimeError(f"gemini HTTP {status}")
                log.warning("gemini %s HTTP %s — next", model, status)
                continue
            saw_success_http = True
            try:
                data = json.loads(body) if body else {}
                est = parse_estimate_json(_candidate_text(data) or body)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning("gemini %s parse miss: %s — next", model, exc)
                continue
            self.last_model = model
            if model != GEMINI_CASCADE[0]:
                log.info("gemini fallback model=%s", model)
            return est
        # Only cool down after we actually got HTTP 200s that still wouldn't parse,
        # or after auth death. Don't cool down solely on 404 model-name misses.
        if saw_success_http or (last_exc and "401" in str(last_exc) or "403" in str(last_exc)):
            self._cool_until = time.time() + COOLDOWN_SECONDS
        raise RuntimeError(f"gemini_cascade_exhausted:{last_exc}")

    async def _do_post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, str]:
        if self._post is not None:
            return await self._post(url, headers, payload)
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            return r.status_code, r.text
