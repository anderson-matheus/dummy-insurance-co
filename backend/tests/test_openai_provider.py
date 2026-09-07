from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from app.core.config import Settings
from app.core.errors import LLMBadResponse, LLMMisconfigured, LLMRateLimited, LLMTimeout, LLMUnavailable
from app.llm.openai_provider import accumulate_chunks, build_request, parse_retry_after, translate_error
from app.llm.types import LLMRequest, MessageEnd, TextDelta, ToolSpec, ToolUse


def chunk(content=None, tool_calls=None, finish=None, usage=None, cid="chatcmpl-1", model="m", error=None):
    delta = NS(content=content, tool_calls=tool_calls)
    c = NS(id=cid, model=model, choices=[NS(delta=delta, finish_reason=finish)] if (content or tool_calls or finish) else [], usage=usage)
    if error:
        c.error = error
    return c


def tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


async def collect(chunks, tool_names=frozenset({"search_documents", "query_claims_db", "refuse"})):
    async def gen():
        for c in chunks:
            yield c

    return [ev async for ev in accumulate_chunks(gen(), set(tool_names))]


async def test_text_stream_and_usage():
    events = await collect([
        chunk(content="A vigência "), chunk(content="é de 12 meses [1]."), chunk(finish="stop"),
        chunk(usage=NS(prompt_tokens=300, completion_tokens=20, prompt_tokens_details=None)),
    ])
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["A vigência ", "é de 12 meses [1]."]
    end = events[-1]
    assert isinstance(end, MessageEnd) and end.stop_reason == "end_turn"
    assert (end.usage.input_tokens, end.usage.output_tokens) == (300, 20)
    assert end.assistant_message == {"role": "assistant", "content": "A vigência é de 12 meses [1]."}
    assert end.request_id == "chatcmpl-1" and end.served_model == "m"


async def test_parallel_tool_calls_accumulated_by_index():
    events = await collect([
        chunk(tool_calls=[tc(0, id="call_a", name="search_documents", args='{"que')]),
        chunk(tool_calls=[tc(1, id="call_b", name="query_claims_db", args='{"sql": "SEL')]),
        chunk(tool_calls=[tc(0, args='ry": "vidros"}'), tc(1, args='ECT 1", "purpose": "x"}')]),
        chunk(finish="tool_calls"),
    ])
    tools = [e for e in events if isinstance(e, ToolUse)]
    assert [(t.id, t.name, t.input) for t in tools] == [
        ("call_a", "search_documents", {"query": "vidros"}),
        ("call_b", "query_claims_db", {"sql": "SELECT 1", "purpose": "x"}),
    ]
    end = events[-1]
    assert end.stop_reason == "tool_use"
    assert end.assistant_message["tool_calls"][1]["function"]["arguments"] == '{"sql": "SELECT 1", "purpose": "x"}'


async def test_stop_with_tool_calls_is_tool_use_and_malformed_args_flagged():
    events = await collect([
        chunk(tool_calls=[tc(0, id="c", name="refuse", args='{"reason_code": ')]),
        chunk(finish="stop"),
    ])
    tool = next(e for e in events if isinstance(e, ToolUse))
    assert tool.input is None and tool.parse_error
    assert events[-1].stop_reason == "tool_use"


async def test_finish_reason_mapping_and_missing_usage():
    assert (await collect([chunk(content="x", finish="length")]))[-1].stop_reason == "max_tokens"
    assert (await collect([chunk(content="x", finish="content_filter")]))[-1].stop_reason == "refusal"
    end = (await collect([chunk(content="x", finish="stop")]))[-1]
    assert end.usage.total == 0


async def test_tool_call_written_as_text_is_salvaged():
    events = await collect([chunk(content='{"name": "search_documents", "arguments": {"query": "franquia vidros"}}'), chunk(finish="stop")])
    tool = next(e for e in events if isinstance(e, ToolUse))
    assert tool.salvaged_from_text and tool.input == {"query": "franquia vidros"}
    assert events[-1].stop_reason == "tool_use" and events[-1].assistant_message["content"] is None


async def test_mid_stream_error_chunk_raises_bad_response():
    with pytest.raises(LLMBadResponse):
        await collect([chunk(content="par"), chunk(finish="error")])
    with pytest.raises(LLMBadResponse):
        await collect([chunk(error={"code": 429, "message": "rate limited"})])


def _status_error(cls, status, headers=None):
    request = httpx2.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls("boom", response=response, body=None)


def test_exception_translation():
    assert isinstance(translate_error(openai.APITimeoutError(request=httpx2.Request("POST", "https://x"))), LLMTimeout)
    assert isinstance(translate_error(openai.APIConnectionError(request=httpx2.Request("POST", "https://x"))), LLMUnavailable)
    rl = translate_error(_status_error(openai.RateLimitError, 429, {"retry-after": "12"}))
    assert isinstance(rl, LLMRateLimited) and rl.retry_after_s == 12.0 and rl.retryable
    assert isinstance(translate_error(_status_error(openai.InternalServerError, 503)), LLMUnavailable)
    auth = translate_error(_status_error(openai.AuthenticationError, 401))
    assert isinstance(auth, LLMMisconfigured) and not auth.retryable
    assert isinstance(translate_error(_status_error(openai.BadRequestError, 400)), LLMMisconfigured)
    assert isinstance(translate_error(ValueError("weird")), LLMBadResponse)
    # user-facing message never carries internals
    assert "boom" not in auth.user_message and "401" not in auth.user_message


def test_parse_retry_after_variants():
    assert parse_retry_after({"retry-after": "3"}) == 3.0
    assert parse_retry_after({"retry-after": "99999"}) == 3600.0
    assert parse_retry_after({}) is None
    assert 0 <= parse_retry_after({"x-ratelimit-reset": str(int(__import__("time").time() * 1000) + 5000)}) <= 6


def test_build_request_shapes():
    s = Settings(llm_model="vendor/model:free", llm_fallback_models="a/b:free,c/d:free", llm_reasoning_effort="low", llm_api_key="k")
    req = LLMRequest(system="sys", messages=[{"role": "user", "content": "oi"}], tools=[ToolSpec("refuse", "d", {"type": "object", "properties": {}})])
    kw = build_request(req, s)
    assert kw["messages"][0] == {"role": "system", "content": "sys"} and kw["stream"] is True
    assert kw["stream_options"] == {"include_usage": True} and kw["tool_choice"] == "auto"
    assert kw["tools"][0]["function"]["name"] == "refuse"
    assert kw["extra_body"]["models"] == ["vendor/model:free", "a/b:free", "c/d:free"]
    assert kw["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}
    kw2 = build_request(LLMRequest(system="s", messages=[]), Settings(llm_model="openrouter/free", llm_reasoning_effort="low", llm_api_key="k"))
    assert "tools" not in kw2 and "extra_body" not in kw2
