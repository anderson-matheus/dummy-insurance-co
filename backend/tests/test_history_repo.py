import pytest

from app.history.repo import Citation, HistoryRepo, Metrics


@pytest.fixture
async def repo():
    r = HistoryRepo(":memory:")
    await r.connect()
    yield r
    await r.close()


async def test_conversation_lifecycle(repo):
    conv = await repo.create_conversation()
    assert conv.title is None
    user = await repo.add_user_message(conv.id, "Qual o prazo de vigência?", "cid-1")
    reply = await repo.add_pending_reply(conv.id, user.id)
    assert (user.seq, reply.seq) == (1, 2)
    assert reply.status == "pending"

    got = await repo.get_conversation(conv.id)
    assert got.title == "Qual o prazo de vigência?"
    assert got.message_count == 2

    final = await repo.finalize_reply(
        reply.id,
        content="12 meses [1]",
        status="complete",
        citations=[Citation(ordinal=1, source_type="document", doc_code="CG-AUTO-2024", page=2)],
        metrics=Metrics(model="m", input_tokens=10, output_tokens=5, provider_request_ids=["r1"]),
        provenance={"index_built_at": "2026-09-07T00:00:00Z"},
    )
    assert final.status == "complete"
    assert final.citations[0].doc_code == "CG-AUTO-2024"
    assert final.metrics.provider_request_ids == ["r1"]
    assert final.provenance["index_built_at"].startswith("2026")

    messages = await repo.get_messages(conv.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].citations[0].page == 2
    assert messages[0].citations == []

    listed = await repo.list_conversations()
    assert listed[0].id == conv.id and listed[0].message_count == 2

    assert await repo.delete_conversation(conv.id) is True
    assert await repo.get_messages(conv.id) == []


async def test_idempotency_lookup_and_retry_in_place(repo):
    conv = await repo.create_conversation()
    user = await repo.add_user_message(conv.id, "pergunta", "cid-42")
    reply = await repo.add_pending_reply(conv.id, user.id)
    await repo.finalize_reply(
        reply.id, content="parcial", status="error", error_code="LLM_TIMEOUT",
        citations=[Citation(ordinal=1, source_type="document")],
        metrics=Metrics(llm_calls=1),
    )
    found = await repo.find_user_message(conv.id, "cid-42")
    assert found.id == user.id
    assert await repo.find_user_message(conv.id, "other") is None

    existing = await repo.get_reply(user.id)
    assert existing.status == "error" and existing.attempt == 1 and existing.citations

    reset = await repo.reset_reply_for_retry(existing.id)
    assert reset.attempt == 2 and reset.status == "pending" and reset.content == ""
    assert reset.error_code is None and reset.citations == [] and reset.metrics is None

    # still exactly two rows: retry never duplicates messages
    assert len(await repo.get_messages(conv.id)) == 2

    # a second reply for the same user message is rejected by the schema
    with pytest.raises(Exception):
        await repo.add_pending_reply(conv.id, user.id)


async def test_sweep_pending_marks_interrupted(repo):
    conv = await repo.create_conversation()
    user = await repo.add_user_message(conv.id, "p", None)
    reply = await repo.add_pending_reply(conv.id, user.id)
    assert await repo.sweep_pending() == 1
    msg = await repo.get_message(reply.id)
    assert msg.status == "interrupted" and msg.error_code == "SERVER_RESTART"


async def test_recent_turns_excludes_failures(repo):
    conv = await repo.create_conversation()
    u1 = await repo.add_user_message(conv.id, "primeira", None)
    r1 = await repo.add_pending_reply(conv.id, u1.id)
    await repo.finalize_reply(r1.id, content="resposta 1", status="complete")
    u2 = await repo.add_user_message(conv.id, "segunda", None)
    r2 = await repo.add_pending_reply(conv.id, u2.id)
    await repo.finalize_reply(r2.id, content="", status="error", error_code="LLM_TIMEOUT")
    u3 = await repo.add_user_message(conv.id, "terceira", None)
    turns = await repo.recent_turns(conv.id, before_seq=u3.seq, limit=4)
    assert [t.content for t in turns] == ["primeira", "resposta 1", "segunda"]
