from __future__ import annotations

from app.ai.credits import CreditHardFail, is_credit_hard_fail
from app.ai.schemas import MarketEstimate


class GrokClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.blocked = False

    async def estimate(self, system_prompt: str, user_prompt: str) -> MarketEstimate:
        if self.blocked:
            raise CreditHardFail("xai_credits_exhausted")
        from xai_sdk import AsyncClient
        from xai_sdk.chat import system, user

        try:
            client = AsyncClient(api_key=self.api_key)
            chat = client.chat.create(
                model=self.model,
                messages=[system(system_prompt)],
            )
            chat.append(user(user_prompt))
            _resp, parsed = await chat.parse(MarketEstimate)
            return parsed
        except CreditHardFail:
            raise
        except Exception as exc:
            if is_credit_hard_fail(exc):
                self.blocked = True
                raise CreditHardFail(str(exc)) from exc
            raise
