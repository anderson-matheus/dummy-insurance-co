"""Provider-neutral request/event types used by the orchestrator and providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMRequest:
    system: str
    messages: list[dict[str, Any]]  # OpenAI chat shapes (user / assistant(+tool_calls) / tool)
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 700
    tool_choice: str = "auto"  # auto | none


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any] | None
    raw_arguments: str
    parse_error: str | None = None
    salvaged_from_text: bool = False


@dataclass
class MessageEnd:
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal
    usage: Usage
    assistant_message: dict[str, Any]
    request_id: str | None = None
    served_model: str | None = None


LLMEvent = Union[TextDelta, ToolUse, MessageEnd]
