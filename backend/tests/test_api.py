import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import LLMTimeout, LLMUnavailable
from app.history.repo import HistoryRepo
from app.knowledge.claims_db import ClaimsDB
from app.knowledge.retriever import Retriever
from app.llm.fake_provider import Fail, FakeProvider, Hang, answer, refuse
from app.main import create_app
from tests.conftest import CLAIMS_DB

VIGENCIA = "Qual é o prazo de vigência padrão de uma apólice de Seguro Auto?"
LONG_PARTIAL = "A vigência padrão é de doze meses, contados a partir das 24 horas da data de início informada na apólice"


@asynccontextmanager
async def make_client(provider, index_path, tmp_path, **overrides):
    kwargs = dict(llm_api_key="k", question_deadline_s=5, llm_max_retries=0, history_db_path=str(tmp_path / "history.db"), claims_db_path=str(CLAIMS_DB))
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    app = create_app(settings=settings, provider=provider, retriever=Retriever(index_path), claims_db=ClaimsDB(str(CLAIMS_DB)))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


async def read_sse(response):
    """Parse SSE frames from an httpx streaming response into (event, data) tuples."""
    frames = []
    event, data = None, []
    async for line in response.aiter_lines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "":
            if event:
                frames.append((event, json.loads("\n".join(data))))
            event, data = None, []
    return frames


async def test_health_and_conversation_crud(index_path, tmp_path):
    async with make_client(FakeProvider([answer("x [1]")]), index_path, tmp_path) as (client, _):
        health = (await client.get("/api/health")).json()
        assert health["status"] == "ok" and health["provider"]["circuit"] == "closed" and health["index"]["documents"] == 13
        assert health["claims_db"]["latest_payment_date"] == "2026-02-11"

        r = await client.post("/api/conversations", json={})
        assert r.status_code == 201
        conv_id = r.json()["id"]
        assert (await client.get("/api/conversations")).json()["items"][0]["id"] == conv_id
        detail = (await client.get(f"/api/conversations/{conv_id}")).json()
        assert detail["messages"] == []
        assert (await client.delete(f"/api/conversations/{conv_id}")).status_code == 204
        missing = await client.get(f"/api/conversations/{conv_id}")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
        assert "correlation_id" in missing.json()["error"]


async def test_send_message_json_persists_answer_and_citations(index_path, tmp_path):
    provider = FakeProvider([answer("A vigência padrão é de 12 meses [1].")])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        r = await client.post(f"/api/conversations/{conv_id}/messages", json={"content": VIGENCIA, "client_message_id": "c1"})
        assert r.status_code == 200
        body = r.json()
        assert body["deduplicated"] is False and body["user_message"]["client_message_id"] == "c1"
        a = body["assistant_message"]
        assert a["status"] == "complete" and a["citations"][0]["doc_code"] == "CG-AUTO-2024"
        assert a["provenance"]["index_built_at"] and a["metrics"]["llm_calls"] == 1
        assert "model" not in a["metrics"]  # model ids are not exposed to clients
        detail = (await client.get(f"/api/conversations/{conv_id}")).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["title"] == VIGENCIA[:80]


async def test_idempotent_resend_does_not_duplicate(index_path, tmp_path):
    provider = FakeProvider([answer("12 meses [1].")])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        body = {"content": VIGENCIA, "client_message_id": "same"}
        first = (await client.post(f"/api/conversations/{conv_id}/messages", json=body)).json()
        second = (await client.post(f"/api/conversations/{conv_id}/messages", json=body)).json()
        assert second["deduplicated"] is True and second["assistant_message"]["id"] == first["assistant_message"]["id"]
        assert len(provider.calls) == 1
        assert len((await client.get(f"/api/conversations/{conv_id}")).json()["messages"]) == 2
        conflict = await client.post(f"/api/conversations/{conv_id}/messages", json={"content": "outra", "client_message_id": "same"})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_failure_then_retry_regenerates_in_place(index_path, tmp_path):
    provider = FakeProvider([Fail(LLMTimeout()), answer("12 meses [1].")])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        body = {"content": VIGENCIA, "client_message_id": "retry-me"}
        first = (await client.post(f"/api/conversations/{conv_id}/messages", json=body)).json()
        a = first["assistant_message"]
        assert a["status"] == "error" and a["error"]["code"] == "LLM_TIMEOUT" and a["error"]["retryable"] is True
        assert "tempo limite" in a["error"]["message"] and "Traceback" not in a["error"]["message"]
        second = (await client.post(f"/api/conversations/{conv_id}/messages", json=body)).json()
        b = second["assistant_message"]
        assert b["id"] == a["id"] and b["attempt"] == 2 and b["status"] == "complete" and b["error"] is None
        msgs = (await client.get(f"/api/conversations/{conv_id}")).json()["messages"]
        assert len(msgs) == 2 and msgs[1]["attempt"] == 2


async def test_refusal_is_a_domain_state(index_path, tmp_path):
    provider = FakeProvider([refuse("NO_SOURCE", "Não há informação sobre desconto à vista nas fontes disponíveis.")])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        r = (await client.post(f"/api/conversations/{conv_id}/messages", json={"content": "Existe desconto à vista?", "client_message_id": "r"})).json()
        a = r["assistant_message"]
        assert a["status"] == "refused" and a["refusal_code"] == "NO_SOURCE" and a["citations"] == []
        assert a["provenance"]["sources_checked"]


async def test_stream_event_order(index_path, tmp_path):
    provider = FakeProvider([answer("A vigência padrão do Seguro Auto é de 12 meses [1], conforme a cláusula de vigência.")])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        async with client.stream("POST", f"/api/conversations/{conv_id}/messages/stream", json={"content": VIGENCIA, "client_message_id": "s1"}) as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            frames = await read_sse(resp)
        names = [f[0] for f in frames]
        assert names[0] == "accepted" and names[1] == "stage" and "sources" in names and "delta" in names and names[-1] == "done"
        text = "".join(d["text"] for e, d in frames if e == "delta")
        done = frames[-1][1]["message"]
        assert text == done["content"] and done["status"] == "complete"
        # resend with the same id: replayed from history, no new LLM call
        async with client.stream("POST", f"/api/conversations/{conv_id}/messages/stream", json={"content": VIGENCIA, "client_message_id": "s1"}) as resp:
            replay = await read_sse(resp)
        assert [f[0] for f in replay] == ["accepted", "done"] and replay[0][1]["deduplicated"] is True
        assert len(provider.calls) == 1


async def test_stream_mid_way_failure_persists_partial(index_path, tmp_path):
    provider = FakeProvider([Fail(LLMUnavailable(), after_text=LONG_PARTIAL)])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        async with client.stream("POST", f"/api/conversations/{conv_id}/messages/stream", json={"content": VIGENCIA, "client_message_id": "f1"}) as resp:
            frames = await read_sse(resp)
        assert frames[-1][0] == "error"
        err = frames[-1][1]
        assert err["code"] == "LLM_UNAVAILABLE" and err["retryable"] is True
        assert err["message"]["status"] == "error" and err["message"]["content"].startswith("A vigência padrão")
        assert any(e == "delta" for e, _ in frames)
        stored = (await client.get(f"/api/conversations/{conv_id}")).json()["messages"][1]
        assert stored["status"] == "error" and stored["error"]["code"] == "LLM_UNAVAILABLE" and stored["content"] == err["message"]["content"]


async def test_cancel_persists_cancelled_partial(index_path, tmp_path):
    provider = FakeProvider([Hang(60)])
    async with make_client(provider, index_path, tmp_path, question_deadline_s=30) as (client, app):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]

        async def consume():
            async with client.stream("POST", f"/api/conversations/{conv_id}/messages/stream", json={"content": VIGENCIA, "client_message_id": "h1"}) as resp:
                return await read_sse(resp)

        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0.02)
            msgs = (await client.get(f"/api/conversations/{conv_id}")).json()["messages"]
            if len(msgs) == 2 and app.state.ctx.runs.get(msgs[1]["id"]):
                break
        assistant_id = msgs[1]["id"]
        r = await client.post(f"/api/conversations/{conv_id}/messages/{assistant_id}/cancel")
        assert r.status_code == 202 and r.json()["cancelled"] is True
        frames = await asyncio.wait_for(task, 5)
        assert frames[-1][0] == "error" and frames[-1][1]["code"] == "CANCELLED"
        stored = (await client.get(f"/api/conversations/{conv_id}")).json()["messages"][1]
        assert stored["status"] == "cancelled"
        # retrying after a cancel reuses the same rows
        provider.turns = [answer("12 meses [1].")]
        again = (await client.post(f"/api/conversations/{conv_id}/messages", json={"content": VIGENCIA, "client_message_id": "h1"})).json()
        assert again["assistant_message"]["id"] == assistant_id and again["assistant_message"]["attempt"] == 2


async def test_validation_and_unknown_conversation(index_path, tmp_path):
    async with make_client(FakeProvider([answer("x [1]")]), index_path, tmp_path) as (client, _):
        r = await client.post("/api/conversations/nope/messages", json={"content": "oi", "client_message_id": "1"})
        assert r.status_code == 404
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        r = await client.post(f"/api/conversations/{conv_id}/messages", json={"content": ""})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


async def test_health_reports_open_circuit(index_path, tmp_path):
    provider = FakeProvider([Fail(LLMUnavailable())])
    async with make_client(provider, index_path, tmp_path, cb_failure_threshold=2) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        for i in range(2):
            await client.post(f"/api/conversations/{conv_id}/messages", json={"content": VIGENCIA, "client_message_id": f"c{i}"})
        health = (await client.get("/api/health")).json()
        assert health["status"] == "degraded" and health["provider"]["circuit"] == "open"
        # while open, a new question fails fast without touching the provider
        calls = len(provider.calls)
        r = (await client.post(f"/api/conversations/{conv_id}/messages", json={"content": VIGENCIA, "client_message_id": "c9"})).json()
        assert r["assistant_message"]["error"]["code"] == "LLM_UNAVAILABLE" and len(provider.calls) == calls


async def test_json_error_carries_retry_after(index_path, tmp_path):
    from app.core.errors import LLMRateLimited

    provider = FakeProvider([Fail(LLMRateLimited(retry_after_s=42))])
    async with make_client(provider, index_path, tmp_path) as (client, _):
        conv_id = (await client.post("/api/conversations", json={})).json()["id"]
        r = (await client.post(f"/api/conversations/{conv_id}/messages", json={"content": VIGENCIA, "client_message_id": "rl"})).json()
        err = r["assistant_message"]["error"]
        assert err["code"] == "LLM_RATE_LIMITED" and err["retry_after_s"] == 42 and err["retryable"] is True
