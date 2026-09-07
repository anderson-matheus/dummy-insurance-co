"""API schemas. Model identifiers are deliberately not exposed to clients."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import describe_error
from app.history.repo import Conversation, Message


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool
    retry_after_s: float | None = None


class CitationOut(BaseModel):
    ordinal: int
    source_type: str
    doc_code: str | None = None
    doc_title: str | None = None
    doc_version: str | None = None
    doc_status: str | None = None
    section: str | None = None
    page: int | None = None
    snippet: str | None = None
    sql: str | None = None
    row_count: int | None = None
    data_snapshot: str | None = None


class MetricsOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    llm_calls: int
    retries: int
    tool_calls: int
    pii_hits: int


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    status: str
    attempt: int
    client_message_id: str | None = None
    error: ErrorInfo | None = None
    refusal_code: str | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    metrics: MetricsOut | None = None
    provenance: dict[str, Any] | None = None
    created_at: str
    completed_at: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str
    message_count: int = 0


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationListOut(BaseModel):
    items: list[ConversationOut]


class CreateConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=1, max_length=64)


class SendMessageOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    deduplicated: bool = False


def message_out(m: Message) -> MessageOut:
    error = None
    if m.error_code and m.status in {"error", "cancelled", "interrupted"}:
        text, retryable = describe_error(m.error_code)
        error = ErrorInfo(code=m.error_code, message=text, retryable=retryable)
    metrics = None
    if m.metrics is not None:
        mm = m.metrics
        metrics = MetricsOut(
            input_tokens=mm.input_tokens, output_tokens=mm.output_tokens, cost_usd=mm.cost_usd, latency_ms=mm.latency_ms,
            llm_calls=mm.llm_calls, retries=mm.retries, tool_calls=mm.tool_calls, pii_hits=mm.pii_hits,
        )
    return MessageOut(
        id=m.id, role=m.role, content=m.content, status=m.status, attempt=m.attempt,
        client_message_id=m.client_message_id, error=error, refusal_code=m.refusal_code,
        citations=[CitationOut(**c.__dict__) for c in m.citations], metrics=metrics, provenance=m.provenance,
        created_at=m.created_at, completed_at=m.completed_at,
    )


def conversation_out(c: Conversation) -> ConversationOut:
    return ConversationOut(id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at, message_count=c.message_count)
