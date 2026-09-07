from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.deps import AppState, get_state
from app.api.schemas import (
    ConversationDetailOut,
    ConversationListOut,
    ConversationOut,
    CreateConversationIn,
    conversation_out,
    message_out,
)
from app.core.errors import NotFound

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation(body: CreateConversationIn | None = None, state: AppState = Depends(get_state)) -> ConversationOut:
    conv = await state.repo.create_conversation(body.title if body else None)
    return conversation_out(conv)


@router.get("", response_model=ConversationListOut)
async def list_conversations(state: AppState = Depends(get_state)) -> ConversationListOut:
    return ConversationListOut(items=[conversation_out(c) for c in await state.repo.list_conversations()])


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(conversation_id: str, state: AppState = Depends(get_state)) -> ConversationDetailOut:
    conv = await state.repo.get_conversation(conversation_id)
    if conv is None:
        raise NotFound(detail=f"conversation {conversation_id}")
    messages = await state.repo.get_messages(conversation_id)
    return ConversationDetailOut(**conversation_out(conv).model_dump(), messages=[message_out(m) for m in messages])


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, state: AppState = Depends(get_state)) -> Response:
    if not await state.repo.delete_conversation(conversation_id):
        raise NotFound(detail=f"conversation {conversation_id}")
    return Response(status_code=204)
