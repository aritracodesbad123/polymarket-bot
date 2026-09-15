from __future__ import annotations

from pydantic import ValidationError

from app.ai.grok_client import GrokClient
from app.ai.prompt_manager import PromptManager
from app.ai.schemas import MarketEstimate
from app.research.researcher import EvidencePacket


class InvalidEstimate(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProbabilityEngine:
    def __init__(self, grok: GrokClient, prompts: PromptManager, model: str) -> None:
        self.grok = grok
        self.prompts = prompts
        self.model = model

    async def estimate(self, packet: EvidencePacket) -> MarketEstimate:
        user_block = self.prompts.render(
            market_id=packet.market_id,
            evidence=packet.to_prompt_block(),
        )
        try:
            est = await self.grok.estimate(self.prompts.body, user_block)
        except ValidationError as exc:
            raise InvalidEstimate(f"malformed_grok_output:{exc}") from exc
        except Exception as exc:
            raise InvalidEstimate(f"grok_error:{exc}") from exc
        if est.market_id and est.market_id != packet.market_id:
            est.market_id = packet.market_id
        if not (0.0 <= est.estimated_probability <= 1.0):
            raise InvalidEstimate("probability_out_of_range")
        return est
