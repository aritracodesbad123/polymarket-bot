from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, Field

from app.ai.credits import CreditHardFail, is_credit_hard_fail


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceItem(BaseModel):
    text: str
    source_url: str = ""
    source_timestamp: datetime | None = None
    kind: str = "report"  # primary, official, government, reputable, base_rate, counter
    is_counter: bool = False


class EvidencePacket(BaseModel):
    market_id: str
    question: str
    resolution_criteria: str
    market_price: float | None = None
    implied_probability: float | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    items: list[EvidenceItem] = Field(default_factory=list)
    base_rate_note: str = ""

    def to_prompt_block(self) -> str:
        lines = [
            f"Question: {self.question}",
            f"Resolution criteria: {self.resolution_criteria}",
            f"Market implied probability: {self.implied_probability}",
            f"Packet time (UTC): {self.timestamp.isoformat()}",
            f"Base rate: {self.base_rate_note or 'not provided'}",
            "Evidence:",
        ]
        if not self.items:
            lines.append(
                "(none) No live search packet. Estimate from resolution criteria, "
                "base rates, and market implied probability. Do not abstain solely "
                "because this list is empty."
            )
        for i, it in enumerate(self.items, 1):
            tag = "COUNTER" if it.is_counter else it.kind
            ts = it.source_timestamp.isoformat() if it.source_timestamp else "unknown"
            lines.append(f"{i}. [{tag}] {it.text} (src={it.source_url or 'n/a'} ts={ts})")
        return "\n".join(lines)


class ResearchProvider(Protocol):
    async def gather(self, packet: EvidencePacket) -> EvidencePacket: ...


class NullResearchProvider:
    """No network. Packet passes through. Gate can still reject empty evidence."""

    async def gather(self, packet: EvidencePacket) -> EvidencePacket:
        return packet


class XAISearchProvider:
    """Uses xAI web_search / x_search only. Never places trades."""

    def __init__(self, api_key: str, model: str = "grok-4.6") -> None:
        self.api_key = api_key
        self.model = model
        self.blocked = False

    async def gather(self, packet: EvidencePacket) -> EvidencePacket:
        if self.blocked:
            raise CreditHardFail("xai_credits_exhausted")
        from xai_sdk import AsyncClient
        from xai_sdk.chat import system, user
        from xai_sdk.tools import web_search, x_search

        try:
            client = AsyncClient(api_key=self.api_key)
            chat = client.chat.create(
                model=self.model,
                tools=[web_search(), x_search()],
                messages=[
                    system(
                        "Collect brief evidence for a prediction-market question. "
                        "Prefer primary sources, official announcements, government data, "
                        "reputable reporting. Include counter-evidence. "
                        "Return 4-8 short bullets with URLs. Do not estimate a probability. "
                        "Do not recommend a trade."
                    ),
                ],
            )
            chat.append(
                user(
                    f"Find recent evidence for this market. Do not give a probability.\n"
                    f"{packet.to_prompt_block()}"
                )
            )
            # ponytail: agent loop capped at 6 tool rounds; raise if search quality is poor
            for _ in range(6):
                response = await chat.sample()
                if not getattr(response, "tool_calls", None):
                    text = getattr(response, "content", "") or ""
                    if text.strip():
                        packet.items.append(
                            EvidenceItem(
                                text=text.strip()[:4000],
                                source_url="xai:web_search",
                                source_timestamp=utcnow(),
                                kind="reputable",
                            )
                        )
                    return packet
                chat.append(response)
            return packet
        except CreditHardFail:
            raise
        except Exception as exc:
            if is_credit_hard_fail(exc):
                self.blocked = True
                raise CreditHardFail(str(exc)) from exc
            raise
