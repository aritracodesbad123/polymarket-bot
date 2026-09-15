from __future__ import annotations

from app.ai.schemas import MarketEstimate


class GrokClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def estimate(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
        from xai_sdk import AsyncClient
        from xai_sdk.chat import system, user

        client = AsyncClient(api_key=self.api_key)
        chat = client.chat.create(
            model=self.model,
            messages=[system(system_prompt)],
        )
        chat.append(user(user_prompt))
        _resp, parsed = await chat.parse(MarketEstimate)
        return parsed
