from __future__ import annotations

import asyncio
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

# Vertex publisher API (x-goog-api-key) — NOT AI Studio / generativelanguage.
# Prefer gemini-2.5-flash under Vertex 429 pressure; still try 3.6-flash later in the cascade.
# HTTP 429 falls through in the SAME call; sticky-skip keeps hot models out of the first slot. Sticky skip keeps a hot 429 model out of the first
# slot for a short window so we don't re-burn it every estimate.
GEMINI_CASCADE = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
)
STICKY_429_SECONDS = 120.0

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
        self._lock = asyncio.Lock()  # serialize calls — concurrent hammer → blank httpx fails + cooldown
        self._skip_until: dict[str, float] = {}  # model -> epoch when 429 sticky expires

    async def estimate(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
        async with self._lock:
            return await self._estimate_unlocked(system_prompt, user_prompt)

    async def _estimate_unlocked(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
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
        now = time.time()
        primary = [m for m in gemini_cascade() if self._skip_until.get(m, 0) <= now]
        deferred = [m for m in gemini_cascade() if self._skip_until.get(m, 0) > now]
        for model in primary + deferred:
            url = f"{VERTEX}/{model}:generateContent"
            log.warning("gemini attempt model=%s", model)
            try:
                status, body = await self._do_post(url, headers, payload)
            except Exception as exc:
                last_exc = exc
                log.warning("gemini %s failed: %r — next", model, exc)
                continue
            if status in (401, 403):
                last_exc = RuntimeError(f"gemini HTTP {status}")
                log.warning("gemini %s HTTP %s body_head=%r — abort cascade", model, status, (body or "")[:240])
                break
            if status == 404:
                last_exc = RuntimeError(f"gemini HTTP 404")
                log.warning("gemini %s HTTP 404 body_head=%r — next", model, (body or "")[:240])
                continue
            if status == 429:
                last_exc = RuntimeError(f"gemini HTTP 429")
                self._skip_until[model] = time.time() + STICKY_429_SECONDS
                log.warning(
                    "gemini %s HTTP 429 — sticky-skip %ss, try next model body_head=%r",
                    model,
                    int(STICKY_429_SECONDS),
                    (body or "")[:240],
                )
                continue
            if status >= 400:
                last_exc = RuntimeError(f"gemini HTTP {status}")
                log.warning("gemini %s HTTP %s body_head=%r — next", model, status, (body or "")[:240])
                continue
            saw_success_http = True
            try:
                data = json.loads(body) if body else {}
                est = parse_estimate_json(_candidate_text(data) or body)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning("gemini %s parse miss: %r body_head=%r — next", model, exc, (body or "")[:240])
                continue
            self.last_model = model
            if model != GEMINI_CASCADE[0]:
                log.info("gemini fallback model=%s", model)
            return est
        # Only cool down after we actually got HTTP 200s that still wouldn't parse,
        # or after auth death. Don't cool down solely on 404 model-name misses.
        # Auth death or successful-but-unusable responses → full cooldown.
        # Pure 429/timeout exhaustion → shorter cool so we retry sooner under quota pressure.
        msg = repr(last_exc)
        if last_exc and ("401" in msg or "403" in msg):
            self._cool_until = time.time() + COOLDOWN_SECONDS
        elif saw_success_http:
            self._cool_until = time.time() + COOLDOWN_SECONDS
        elif last_exc and ("429" in msg or "timeout" in msg.lower() or "Timeout" in msg):
            self._cool_until = time.time() + min(30.0, COOLDOWN_SECONDS)
        raise RuntimeError(f"gemini_cascade_exhausted:{last_exc}")

    async def _do_post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, str]:
        if self._post is not None:
            return await self._post(url, headers, payload)
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            return r.status_code, r.text
