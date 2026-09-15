import json

import pytest

from app.ai.gemini_client import GEMINI_2XX, GEMINI_3XX, GeminiClient, gemini_cascade
from app.ai.schemas import MarketEstimate
from tests.conftest import estimate


def _vertex_ok(est: MarketEstimate | None = None) -> str:
    body = (est or estimate()).model_dump_json()
    return json.dumps({"candidates": [{"content": {"parts": [{"text": body}]}}]})


def test_cascade_3xx_before_2xx():
    chain = gemini_cascade()
    assert chain[0] == "gemini-3.1-pro"
    assert chain[: len(GEMINI_3XX)] == GEMINI_3XX
    assert chain[len(GEMINI_3XX) :] == GEMINI_2XX
    assert all(not m.startswith("gemini-2.") for m in GEMINI_3XX)


@pytest.mark.asyncio
async def test_3_1_pro_success_skips_rest():
    urls: list[str] = []

    async def post(url, _headers, _payload):
        urls.append(url)
        return 200, _vertex_ok()

    est = await GeminiClient("k", post=post).estimate("sys", "user")
    assert est.estimated_probability == 0.7
    assert len(urls) == 1
    assert "gemini-3.1-pro:generateContent" in urls[0]
    assert not any("gemini-2." in u for u in urls)


@pytest.mark.asyncio
async def test_all_3xx_404_then_2xx():
    urls: list[str] = []

    async def post(url, _headers, _payload):
        urls.append(url)
        if GEMINI_2XX[0] in url:
            return 200, _vertex_ok()
        return 404, "not found"

    est = await GeminiClient("k", post=post).estimate("sys", "user")
    assert est.estimated_probability == 0.7
    models = [u.rsplit("/", 1)[-1].split(":")[0] for u in urls]
    assert models[: len(GEMINI_3XX)] == list(GEMINI_3XX)
    assert models[len(GEMINI_3XX)] == GEMINI_2XX[0]
    assert models[len(GEMINI_3XX)].startswith("gemini-2.")


@pytest.mark.asyncio
async def test_cascade_exhausted_raises():
    n = 0

    async def post(_url, _headers, _payload):
        nonlocal n
        n += 1
        return 404, "not found"

    with pytest.raises(RuntimeError, match="gemini_cascade_exhausted"):
        await GeminiClient("k", post=post).estimate("sys", "user")
    assert n == len(gemini_cascade())
