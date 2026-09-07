"""The provider seam: anything that streams a turn as our events."""
from __future__ import annotations

from typing import AsyncIterator, Protocol

from app.llm.types import LLMEvent, LLMRequest


class LLMProvider(Protocol):
    name: str

    def stream_turn(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:  # pragma: no cover - protocol
        ...
