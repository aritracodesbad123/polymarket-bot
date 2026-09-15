from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from app.ai.credits import CreditHardFail
from app.ai.gemini_client import GeminiClient
from app.ai.grok_client import GrokClient
from app.ai.prompt_manager import PromptManager
from app.ai.schemas import MarketEstimate
from app.research.researcher import EvidencePacket


class InvalidEstimate(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProbabilityEngine:
    def __init__(
        self,
        grok: GrokClient | None,
        prompts: PromptManager,
        gemini: GeminiClient | None = None,
        on_provider_swap: Callable[[str], None] | None = None,
    ) -> None:
        self.grok = grok
        self.gemini = gemini
        self.prompts = prompts
        self.on_provider_swap = on_provider_swap
        self.provider = "grok" if grok is not None else ("gemini" if gemini is not None else None)
        self._swapped = False

    @property
    def last_model(self) -> str:
        if self.provider == "gemini" and self.gemini is not None:
            return self.gemini.last_model
        if self.grok is not None:
            return self.grok.model
        return ""

    async def estimate(self, packet: EvidencePacket) -> MarketEstimate:
        user_block = self.prompts.render(
            market_id=packet.market_id,
            evidence=packet.to_prompt_block(),
        )
        if self.provider == "grok" and self.grok is not None:
            try:
                return self._finish(await self.grok.estimate(self.prompts.body, user_block), packet)
            except CreditHardFail:
                self._flip_to_gemini()
            except ValidationError as exc:
                raise InvalidEstimate(f"malformed_grok_output:{exc}") from exc
            except Exception as exc:
                raise InvalidEstimate(f"grok_error:{exc}") from exc
        if self.provider == "gemini" and self.gemini is not None:
            try:
                # Always run extreme-p abstain via _finish for Gemini too.
                return self._finish(
                    await self.gemini.estimate(self.prompts.body, user_block), packet
                )
            except ValidationError as exc:
                raise InvalidEstimate(f"malformed_gemini_output:{exc}") from exc
            except Exception as exc:
                raise InvalidEstimate(f"gemini_error:{exc}") from exc
        raise InvalidEstimate("xai_credits_exhausted" if self.grok else "no_provider")

    def _flip_to_gemini(self) -> None:
        if self.gemini is None:
            raise InvalidEstimate("xai_credits_exhausted")
        self.provider = "gemini"
        if not self._swapped:
            self._swapped = True
            if self.on_provider_swap:
                self.on_provider_swap(self.gemini.last_model)

    # Only hard-abstain exact 0/1. Soft near-extremes are gated by strategy edge/confidence.
    # ponytail: was 0.02 band; that burned ~60% of Gemini estimates as grok_abstain with empty search.

    def _finish(self, est: MarketEstimate, packet: EvidencePacket) -> MarketEstimate:
        if est.market_id and est.market_id != packet.market_id:
            est.market_id = packet.market_id
        if not (0.0 <= est.estimated_probability <= 1.0):
            raise InvalidEstimate("probability_out_of_range")
        p = est.estimated_probability
        if p <= 0.0 or p >= 1.0:
            est.should_abstain = True
            est.abstention_reason = f"extreme_probability:{p:.4f}"
        return est
