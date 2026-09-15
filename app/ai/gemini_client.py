from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.ai.schemas import MarketEstimate

log = logging.getLogger(__name__)

VERTEX = "https://aiplatform.googleapis.com/v1/publishers/google/models"
COOLDOWN_SECONDS = 90.0

# Family A then family B. Never call 2.xx until every 3.xx has failed.
GEMINI_3XX = (
    "gemini-3.1-pro",
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
)
GEMINI_2XX = (
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


def gemini_cascade() -> tuple[str, ...]:
    return GEMINI_3XX + GEMINI_2XX


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
    return MarketEstimate.model_validate(obj)


class GeminiClient:
    def __init__(self, api_key: str, post: PostFn | None = None) -> None:
        self.api_key = api_key
        self.last_model = GEMINI_3XX[0]
        self._cool_until = 0.0
        self._post = post

    async def estimate(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
        if time.time() < self._cool_until:
            raise RuntimeError("gemini_cooldown")
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }
        headers = {"content-type": "application/json", "x-goog-api-key": self.api_key}
        last_exc: Exception | None = None
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
            if status >= 400:
                last_exc = RuntimeError(f"gemini HTTP {status}")
                log.warning("gemini %s HTTP %s — next", model, status)
                continue
            try:
                data = json.loads(body) if body else {}
                est = parse_estimate_json(_candidate_text(data) or body)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_exc = exc
                log.warning("gemini %s parse miss — next", model)
                continue
            self.last_model = model
            if model != GEMINI_3XX[0]:
                kind = "2.xx" if model.startswith("gemini-2.") else "3.xx"
                log.info("gemini fallback family=%s model=%s", kind, model)
            return est
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
