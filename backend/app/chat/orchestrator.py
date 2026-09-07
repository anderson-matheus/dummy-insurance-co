"""Per-question pipeline: retrieve -> generate (with tools) -> validate citations -> scrub PII.

``answer_question`` is an async generator of domain events consumed by both the
JSON and the SSE transports. Failures propagate as ``AppError`` subclasses; the
caller reads partial text and metrics from the ``QuestionRun`` it passed in.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.chat.prompts import (
    CITATION_REPAIR_PROMPT,
    NO_SOURCE_MESSAGE,
    PROVIDER_REFUSAL_MESSAGE,
    SYSTEM_PROMPT,
    TOOL_SPECS,
    render_user_block,
)
from app.chat.sources import SourceRegistry
from app.chat.tools import execute_tool
from app.core.config import Settings
from app.core.errors import LLMBadResponse, ResponseTruncated
from app.history.repo import Citation, Message, Metrics
from app.knowledge.claims_db import ClaimsDB
from app.knowledge.pii import PIIScrubber
from app.knowledge.retriever import Retriever
from app.llm.resilient import QuestionBudget, ResilientLLM
from app.llm.types import LLMRequest, MessageEnd, TextDelta, ToolUse

log = logging.getLogger(__name__)

HISTORY_ASSISTANT_CHARS = 600
STREAM_HOLDBACK_CHARS = 40


@dataclass
class Stage:
    name: str  # retrieving | generating | searching_documents | querying_db


@dataclass
class SourcesUpdate:
    sources: list[dict]


@dataclass
class Delta:
    text: str


@dataclass
class Reset:
    pass


@dataclass
class Completed:
    content: str
    status: str  # complete | refused
    refusal_code: str | None
    citations: list[Citation]
    metrics: Metrics
    provenance: dict[str, Any]


Event = Stage | SourcesUpdate | Delta | Reset | Completed


@dataclass
class Deps:
    settings: Settings
    llm: ResilientLLM
    retriever: Retriever
    claims_db: ClaimsDB
    scrubber: PIIScrubber


class StreamScrubber:
    """Emit scrubbed text progressively, holding back a tail so PII spanning deltas never leaks."""

    def __init__(self, scrubber: PIIScrubber, holdback: int = STREAM_HOLDBACK_CHARS) -> None:
        self.scrubber = scrubber
        self.holdback = holdback
        self.raw = ""
        self.emitted = ""
        self.hits = 0

    def reset(self) -> None:
        self.raw = ""
        self.emitted = ""

    def _emit(self, safe: str) -> list[Event]:
        if safe.startswith(self.emitted):
            tail = safe[len(self.emitted) :]
            self.emitted = safe
            return [Delta(tail)] if tail else []
        self.emitted = safe
        return [Reset(), Delta(safe)] if safe else [Reset()]

    def push(self, delta: str) -> list[Event]:
        self.raw += delta
        cut = len(self.raw) - self.holdback
        if cut <= 0:
            return []
        boundary = self.raw.rfind(" ", 0, cut)
        safe_raw = self.raw[: boundary if boundary > 0 else cut]
        safe, _ = self.scrubber.scrub(safe_raw)
        return self._emit(safe)

    def finish(self, final_text: str) -> list[Event]:
        return self._emit(final_text)


@dataclass
class QuestionRun:
    """Mutable per-question state shared with the caller (for partial persistence on failure)."""

    budget: QuestionBudget
    settings: Settings
    started: float = field(default_factory=time.monotonic)
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    tool_calls: int = 0
    pii_hits: int = 0
    partial_text: str = ""
    db_queried: bool = False

    def metrics(self) -> Metrics:
        b = self.budget
        return Metrics(
            model=self.settings.llm_model,
            served_model=", ".join(b.served_models) or None,
            input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            cost_usd=round(b.cost_usd, 6),
            latency_ms=int((time.monotonic() - self.started) * 1000),
            llm_calls=b.llm_calls,
            retries=b.retries,
            tool_calls=self.tool_calls,
            pii_hits=self.pii_hits,
            provider_request_ids=list(b.request_ids),
        )


def new_run(deps: Deps) -> QuestionRun:
    return QuestionRun(budget=deps.llm.new_budget(), settings=deps.settings)


def _history_messages(history: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in history:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant" and m.content:
            out.append({"role": "assistant", "content": m.content[:HISTORY_ASSISTANT_CHARS]})
    return out


def _provenance(deps: Deps, run: QuestionRun, cited: list[Citation], db_queried: bool) -> dict[str, Any]:
    index = deps.retriever.info()
    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in cited:
        if c.source_type == "document" and c.doc_code and (c.doc_code, c.doc_version or "") not in seen:
            seen.add((c.doc_code, c.doc_version or ""))
            docs.append({"doc_code": c.doc_code, "doc_title": c.doc_title, "version": c.doc_version, "status": c.doc_status})
    return {
        "documents_cited": docs,
        "database_queried": db_queried,
        "database_snapshot": deps.claims_db.snapshot_label() if db_queried else None,
        "index_built_at": index.get("built_at"),
        "sources_checked": [s.label() for s in run.registry.all()],
    }


async def answer_question(question: str, history: list[Message], deps: Deps, run: QuestionRun) -> AsyncIterator[Event]:
    s = deps.settings
    registry = run.registry
    streamer = StreamScrubber(deps.scrubber)

    yield Stage("retrieving")
    for chunk in deps.retriever.search(question, k=s.retrieval_top_k):
        registry.add_chunk(chunk)
    yield SourcesUpdate(registry.summaries())

    freshness = f"índice de documentos gerado em {(deps.retriever.info().get('built_at') or '')[:10]}; banco de sinistros com {deps.claims_db.snapshot_label()}"
    messages: list[dict[str, Any]] = _history_messages(history)
    messages.append({"role": "user", "content": render_user_block(question, registry.render_for_model(), freshness)})

    def finish(content: str, status: str, refusal_code: str | None, cited: list[Citation]) -> Completed:
        scrubbed, hits = deps.scrubber.scrub(content)
        run.pii_hits += hits
        run.partial_text = scrubbed
        return Completed(
            content=scrubbed, status=status, refusal_code=refusal_code, citations=cited,
            metrics=run.metrics(), provenance=_provenance(deps, run, cited, run.db_queried),
        )

    try:
        async for ev in _generate(question, messages, deps, run, streamer, finish):
            yield ev
    except BaseException:
        # persist what the model had streamed (scrubbed), including the held-back tail
        run.partial_text = deps.scrubber.scrub(streamer.raw)[0]
        raise


async def _generate(question: str, messages: list[dict[str, Any]], deps: Deps, run: QuestionRun, streamer: StreamScrubber, finish) -> AsyncIterator[Event]:
    s = deps.settings
    registry = run.registry
    tool_choice = "auto"
    iterations = 0
    repairs = 0
    citation_repaired = False

    while True:
        yield Stage("generating")
        req = LLMRequest(system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS, max_tokens=s.llm_max_output_tokens, tool_choice=tool_choice)
        raw_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        end: MessageEnd | None = None
        async for ev in deps.llm.stream_turn(req, run.budget):
            if isinstance(ev, TextDelta):
                raw_parts.append(ev.text)
                for out in streamer.push(ev.text):
                    yield out
                run.partial_text = streamer.emitted
            elif isinstance(ev, ToolUse):
                tool_uses.append(ev)
            elif isinstance(ev, MessageEnd):
                end = ev
        if end is None:
            raise LLMBadResponse(detail="stream ended without a message end")
        text = "".join(raw_parts)

        if end.stop_reason == "refusal":
            for out in streamer.finish(PROVIDER_REFUSAL_MESSAGE):
                yield out
            yield finish(PROVIDER_REFUSAL_MESSAGE, "refused", "PROVIDER_REFUSAL", [])
            return

        if tool_uses and tool_choice != "none":
            if text.strip() or streamer.emitted:
                yield Reset()
                streamer.reset()
                run.partial_text = ""
            messages.append(end.assistant_message)
            iterations += 1
            for tu in tool_uses:
                run.tool_calls += 1
                outcome = await execute_tool(tu, registry, deps.retriever, deps.claims_db, s.tool_search_top_k)
                if outcome.stage:
                    yield Stage(outcome.stage)
                if outcome.refusal is not None:
                    for out in streamer.finish(outcome.refusal.message):
                        yield out
                    yield finish(outcome.refusal.message, "refused", outcome.refusal.reason_code, [])
                    return
                if tu.name == "query_claims_db" and not outcome.is_error:
                    run.db_queried = True
                if outcome.is_error:
                    repairs += 1
                messages.append({"role": "tool", "tool_call_id": tu.id, "content": outcome.content})
                if outcome.new_sources:
                    yield SourcesUpdate(registry.summaries())
            if iterations >= s.max_tool_iterations or repairs > s.max_tool_repairs:
                tool_choice = "none"
            continue

        if end.stop_reason == "max_tokens":
            raise ResponseTruncated(detail=f"max_tokens reached after {len(text)} chars")

        cleaned, cited_sources, dropped = registry.validate_markers(text)
        if dropped:
            log.info("dropped %d invalid citation markers: %s", len(dropped), dropped)
        if "SEM_FONTE" in cleaned.upper():
            cited_sources = []
        if not cited_sources:
            if len(registry) and not citation_repaired and "SEM_FONTE" not in cleaned.upper():
                citation_repaired = True
                messages.append(end.assistant_message)
                messages.append({"role": "user", "content": CITATION_REPAIR_PROMPT})
                tool_choice = "none"
                yield Reset()
                streamer.reset()
                run.partial_text = ""
                continue
            log.info("answer without verifiable citations discarded (%d chars)", len(cleaned))
            for out in streamer.finish(NO_SOURCE_MESSAGE):
                yield out
            yield finish(NO_SOURCE_MESSAGE, "refused", "NO_VERIFIABLE_SOURCE", [])
            return

        final, hits = deps.scrubber.scrub(cleaned)
        run.pii_hits += hits
        for out in streamer.finish(final):
            yield out
        citations = [src.to_citation() for src in cited_sources]
        run.partial_text = final
        yield Completed(
            content=final, status="complete", refusal_code=None, citations=citations,
            metrics=run.metrics(), provenance=_provenance(deps, run, citations, run.db_queried),
        )
        return
