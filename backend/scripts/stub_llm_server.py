"""Offline OpenAI-compatible stub for demos and failure drills (no quota, no key).

Run:  python -m scripts.stub_llm_server  (listens on :8010)
Point the app at it with LLM_BASE_URL=http://localhost:8010/v1 (any LLM_API_KEY).

Behaviour is driven by the analyst's question:
  /timeout   never answers (exercise the deadline)
  /429       HTTP 429 with Retry-After: 6
  /500       HTTP 500
  /midfail   streams a few words, then drops the connection
  /slow      streams slowly (~12 s) so cancel can be exercised
  /nocite    answers without [n] markers (server-side refusal path)
  /pii       answers with a policyholder name and CPF (scrubber path)
  /truncate  finish_reason=length
  "SIN-…"    calls query_claims_db first, then answers citing the result
  nome/cpf/telefone -> refuse PII_REQUEST; desconto/iof -> refuse NO_SOURCE
  otherwise  answers citing source [1]
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="stub-llm")


def _chunk(delta: dict[str, Any], finish: str | None = None, usage: dict | None = None, cid: str = "") -> str:
    body: dict[str, Any] = {
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub/model:free",
        "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _stream(text: str, tool_call: dict | None = None, finish: str = "stop", delay: float = 0.02, drop_after: int | None = None) -> AsyncIterator[str]:
    cid = f"chatcmpl-stub-{uuid.uuid4().hex[:8]}"
    yield _chunk({"role": "assistant", "content": ""}, cid=cid)
    words = text.split(" ")
    for i, w in enumerate(words):
        if drop_after is not None and i >= drop_after:
            raise RuntimeError("simulated connection drop")
        if w:
            yield _chunk({"content": (" " if i else "") + w}, cid=cid)
            await asyncio.sleep(delay)
    if tool_call:
        args = json.dumps(tool_call["arguments"], ensure_ascii=False)
        yield _chunk({"tool_calls": [{"index": 0, "id": f"call_{uuid.uuid4().hex[:6]}", "type": "function", "function": {"name": tool_call["name"], "arguments": args[:12]}}]}, cid=cid)
        yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[12:]}}]}, cid=cid)
        finish = "tool_calls"
    yield _chunk({}, finish=finish, cid=cid)
    yield _chunk({}, usage={"prompt_tokens": 2500, "completion_tokens": max(1, len(words) * 2)}, cid=cid)
    yield "data: [DONE]\n\n"


def _last_user_question(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content") or ""
            if "PERGUNTA DO ANALISTA:" in content:
                return content.split("PERGUNTA DO ANALISTA:", 1)[1].split("\n\n", 1)[0].strip()
            return content.strip()
    return ""


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    question = _last_user_question(messages)
    q = question.lower()
    last = messages[-1] if messages else {}

    if last.get("role") == "tool":
        m = re.search(r"fonte \[(\d+)\]", last.get("content", ""))
        n = m.group(1) if m else "1"
        rows = last.get("content", "").split("\n")[1:3]
        return StreamingResponse(_stream(f"Segundo o banco de sinistros, {' / '.join(rows)} [{n}]. O limite da cobertura consta da tabela de coberturas [1]."), media_type="text/event-stream")
    if q.startswith("/timeout"):
        await asyncio.sleep(120)
    if q.startswith("/429"):
        return JSONResponse({"error": {"message": "Rate limit exceeded", "code": 429}}, status_code=429, headers={"Retry-After": "6"})
    if q.startswith("/500"):
        return JSONResponse({"error": {"message": "internal", "code": 500}}, status_code=500)
    if q.startswith("/midfail"):
        return StreamingResponse(_stream("A vigência padrão é de doze meses contados a partir da data de início informada na apólice e a renovação não é automática [1]", drop_after=14), media_type="text/event-stream")
    if q.startswith("/slow"):
        return StreamingResponse(_stream("Esta resposta é transmitida lentamente para demonstrar o cancelamento pelo usuário e o temporizador da interface [1].", delay=0.6), media_type="text/event-stream")
    if q.startswith("/nocite"):
        return StreamingResponse(_stream("A vigência padrão é de 12 meses, mas esta resposta não cita fonte alguma."), media_type="text/event-stream")
    if q.startswith("/pii"):
        return StreamingResponse(_stream("O sinistro de Marta Ferreira Bittencourt, CPF 111.111.111-11, foi pago no limite da cobertura [1]."), media_type="text/event-stream")
    if q.startswith("/truncate"):
        return StreamingResponse(_stream("A vigência padrão é de 12 meses, contados a partir das 24 horas da data de", finish="length"), media_type="text/event-stream")
    if any(w in q for w in ("cpf", "nome completo", "telefone", "e-mail", "email")):
        return StreamingResponse(_stream("", tool_call={"name": "refuse", "arguments": {"reason_code": "PII_REQUEST", "message": "Não posso expor dados pessoais de segurados (nome, CPF, telefone), conforme a Política de Privacidade POL-LGPD-2024. Posso informar o número e o status dos sinistros citados."}}), media_type="text/event-stream")
    if any(w in q for w in ("desconto", "iof", "bom dia", "tudo bem")):
        return StreamingResponse(_stream("", tool_call={"name": "refuse", "arguments": {"reason_code": "NO_SOURCE", "message": "Não há informação sobre esse assunto nos documentos e no banco de sinistros disponíveis."}}), media_type="text/event-stream")
    sin = re.search(r"SIN-\d{4}-\d{6}", question)
    if sin and body.get("tool_choice") != "none":
        sql = f"SELECT c.claim_number, c.status, c.claim_amount, p.paid_amount FROM claims c LEFT JOIN payments p ON p.claim_id = c.claim_id WHERE c.claim_number = '{sin.group(0)}'"
        return StreamingResponse(_stream("", tool_call={"name": "query_claims_db", "arguments": {"sql": sql, "purpose": "valor reivindicado e pago"}}), media_type="text/event-stream")
    return StreamingResponse(_stream("Resposta simulada pelo provedor de demonstração: a informação solicitada consta da primeira fonte listada [1]. Esta resposta serve para validar a interface e o fluxo completo sem consumir cota do provedor real [2]."), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010, log_level="warning")
