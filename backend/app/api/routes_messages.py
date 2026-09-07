"""Ask a question: JSON (collect) or SSE (stream) transports over the same detached run."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends

from app.api.deps import AppState, get_state
from app.api.schemas import SendMessageIn, SendMessageOut, message_out
from app.api.sse import format_sse, sse_response
from app.chat.orchestrator import Completed, Delta, Reset, SourcesUpdate, Stage, answer_question, new_run
from app.core.errors import AppError, Cancelled, Conflict, NotFound
from app.core.logging import correlation_id, new_correlation_id
from app.history.repo import Message
from app.runs import Run

log = logging.getLogger(__name__)
router = APIRouter(prefix="/conversations/{conversation_id}/messages", tags=["messages"])


async def _prepare(state: AppState, conversation_id: str, body: SendMessageIn) -> tuple[Message, Message, bool]:
    """Idempotent turn preparation: returns (user_message, assistant_message, deduplicated)."""
    repo = state.repo
    if await repo.get_conversation(conversation_id) is None:
        raise NotFound(detail=f"conversation {conversation_id}")
    user = await repo.find_user_message(conversation_id, body.client_message_id)
    if user is None:
        user = await repo.add_user_message(conversation_id, body.content, body.client_message_id)
        reply = await repo.add_pending_reply(conversation_id, user.id)
        return user, reply, False
    if user.content != body.content:
        raise Conflict(detail="same client_message_id with different content")
    reply = await repo.get_reply(user.id)
    if reply is None:
        reply = await repo.add_pending_reply(conversation_id, user.id)
        return user, reply, False
    if reply.status in {"complete", "refused"}:
        return user, reply, True
    if reply.status == "pending" and state.runs.get(reply.id) is not None:
        return user, reply, True  # still running: caller attaches to the run
    reply = await repo.reset_reply_for_retry(reply.id)
    return user, reply, False


def _start_run(state: AppState, user: Message, reply: Message) -> Run:
    cid = correlation_id.get()

    async def worker(run: Run) -> None:
        correlation_id.set(cid)
        repo = state.repo
        history = await repo.recent_turns(user.conversation_id, before_seq=user.seq, limit=state.settings.history_turns)
        qrun = new_run(state.deps)
        await run.publish("accepted", {"user_message_id": user.id, "assistant_message_id": reply.id, "attempt": reply.attempt})
        try:
            async for ev in answer_question(user.content, history, state.deps, qrun):
                if isinstance(ev, Stage):
                    await run.publish("stage", {"stage": ev.name})
                elif isinstance(ev, SourcesUpdate):
                    await run.publish("sources", {"sources": ev.sources})
                elif isinstance(ev, Delta):
                    await run.publish("delta", {"text": ev.text})
                elif isinstance(ev, Reset):
                    await run.publish("reset", {})
                elif isinstance(ev, Completed):
                    final = await repo.finalize_reply(
                        reply.id, content=ev.content, status=ev.status, refusal_code=ev.refusal_code,
                        citations=ev.citations, metrics=ev.metrics, provenance=ev.provenance,
                    )
                    await run.publish("done", {"message": message_out(final).model_dump()})
        except asyncio.CancelledError:
            final = await repo.finalize_reply(reply.id, content=qrun.partial_text, status="cancelled", error_code="CANCELLED", metrics=qrun.metrics())
            await run.publish("error", {**Cancelled().to_dict(), "message": message_out(final).model_dump()})
            raise
        except AppError as err:
            log.warning("question failed: %s (%s)", err.code, err.detail)
            final = await repo.finalize_reply(reply.id, content=qrun.partial_text, status="error", error_code=err.code, metrics=qrun.metrics())
            await run.publish("error", {**err.to_dict(), "message": message_out(final).model_dump()})
        except Exception as err:  # noqa: BLE001 - never leak internals, never leave a pending row
            log.exception("unexpected failure answering question")
            generic = AppError(detail=str(err))
            final = await repo.finalize_reply(reply.id, content=qrun.partial_text, status="error", error_code="INTERNAL", metrics=qrun.metrics())
            await run.publish("error", {**generic.to_dict(), "message": message_out(final).model_dump()})

    return state.runs.start(reply.id, worker)


@router.post("", response_model=SendMessageOut)
async def send_message(conversation_id: str, body: SendMessageIn, state: AppState = Depends(get_state)) -> SendMessageOut:
    user, reply, dedup = await _prepare(state, conversation_id, body)
    if dedup and reply.status in {"complete", "refused"}:
        return SendMessageOut(user_message=message_out(user), assistant_message=message_out(reply), deduplicated=True)
    run = state.runs.get(reply.id) if dedup else _start_run(state, user, reply)
    if run is not None:
        await run.wait()
    final = await state.repo.get_message(reply.id)
    return SendMessageOut(user_message=message_out(user), assistant_message=message_out(final), deduplicated=dedup)


@router.post("/stream")
async def stream_message(conversation_id: str, body: SendMessageIn, state: AppState = Depends(get_state)):
    user, reply, dedup = await _prepare(state, conversation_id, body)

    async def frames() -> AsyncIterator[str]:
        seq = 0
        if dedup and reply.status in {"complete", "refused"}:
            yield format_sse("accepted", {"user_message_id": user.id, "assistant_message_id": reply.id, "deduplicated": True}, seq)
            yield format_sse("done", {"message": message_out(reply).model_dump()}, seq + 1)
            return
        run = state.runs.get(reply.id) if dedup else _start_run(state, user, reply)
        if run is None:  # pending row whose run vanished: treat as retry
            _, fresh, _ = await _prepare(state, conversation_id, body)
            run = _start_run(state, user, fresh)
        async for item in run.subscribe():
            if item is None:
                yield ": ping\n\n"
                continue
            seq += 1
            yield format_sse(item[0], item[1], seq)

    return sse_response(frames())


@router.post("/{assistant_id}/cancel", status_code=202)
async def cancel_message(conversation_id: str, assistant_id: str, state: AppState = Depends(get_state)) -> dict:
    msg = await state.repo.get_message(assistant_id)
    if msg is None or msg.conversation_id != conversation_id:
        raise NotFound(detail=f"message {assistant_id}")
    cancelled = state.runs.cancel(assistant_id)
    if not cancelled and msg.status == "pending":  # orphan (e.g. after restart)
        await state.repo.finalize_reply(assistant_id, content="", status="cancelled", error_code="CANCELLED")
        cancelled = True
    return {"cancelled": cancelled, "message_id": assistant_id}
