"""OpenAI-compatible Chat Completions provider (OpenRouter, Groq, Gemini, Ollama, ...).

Isolation rules:
- the SDK is only imported here; everything else sees ``LLMError`` subclasses
  and the events in ``llm/types.py``;
- ``accumulate_chunks`` is a pure function over stream chunks so the parsing
  logic is unit-tested without a network or the SDK's HTTP layer;
- SDK retries are disabled (``max_retries=0``): ``ResilientLLM`` is the single
  retry authority so the per-question budget stays predictable.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterable, AsyncIterator

import openai
from openai import AsyncOpenAI, Timeout

from app.core.config import Settings
from app.core.errors import (
    LLMBadResponse,
    LLMError,
    LLMMisconfigured,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.types import LLMEvent, LLMRequest, MessageEnd, TextDelta, ToolUse, Usage

log = logging.getLogger(__name__)

FINISH_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


def build_request(req: LLMRequest, settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": req.system}, *req.messages],
        "max_tokens": req.max_tokens,
        "temperature": settings.llm_temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        kwargs["tools"] = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in req.tools
        ]
        kwargs["tool_choice"] = req.tool_choice
    extra: dict[str, Any] = {}
    if settings.fallback_models:
        extra["models"] = [settings.llm_model, *settings.fallback_models][:4]
    if settings.llm_reasoning_effort and not settings.llm_model.startswith("openrouter/"):
        extra["reasoning"] = {"effort": settings.llm_reasoning_effort, "exclude": True}
    if extra:
        kwargs["extra_body"] = extra
    return kwargs


def parse_retry_after(headers: Any) -> float | None:
    """Seconds to wait, from Retry-After or X-RateLimit-Reset (epoch seconds/ms), capped to an hour."""
    if headers is None:
        return None
    try:
        ra = headers.get("retry-after")
        if ra:
            return max(0.0, min(float(ra), 3600.0))
        reset = headers.get("x-ratelimit-reset")
        if reset:
            value = float(reset)
            now = time.time()
            if value > 1e12:
                value = value / 1000.0 - now
            elif value > 1e9:
                value = value - now
            return max(0.0, min(value, 3600.0))
    except (TypeError, ValueError):
        return None
    return None


def translate_error(exc: Exception) -> LLMError:
    """Map SDK exceptions to our taxonomy; details go to logs, never to users."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, openai.APITimeoutError):  # subclass of APIConnectionError: check first
        return LLMTimeout(detail=str(exc))
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailable(detail=str(exc))
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited(detail=f"429 {exc.message}", retry_after_s=parse_retry_after(getattr(exc, "response", None) and exc.response.headers))
    if isinstance(exc, openai.InternalServerError):
        return LLMUnavailable(detail=f"{exc.status_code} {exc.message}")
    if isinstance(exc, openai.APIStatusError):
        return LLMMisconfigured(detail=f"{exc.status_code} {exc.message}")
    return LLMBadResponse(detail=f"{type(exc).__name__}: {exc}")


async def accumulate_chunks(chunks: AsyncIterable[Any], tool_names: set[str]) -> AsyncIterator[LLMEvent]:
    """Turn raw Chat Completions stream chunks into TextDelta / ToolUse / MessageEnd."""
    text_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage = Usage()
    finish: str | None = None
    request_id: str | None = None
    served_model: str | None = None
    async for chunk in chunks:
        err = getattr(chunk, "error", None) or (getattr(chunk, "model_extra", None) or {}).get("error")
        if err:
            raise LLMBadResponse(detail=f"provider stream error: {err}")
        request_id = request_id or getattr(chunk, "id", None)
        served_model = served_model or getattr(chunk, "model", None)
        cu = getattr(chunk, "usage", None)
        if cu:
            usage = Usage(
                input_tokens=int(getattr(cu, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(cu, "completion_tokens", 0) or 0),
                cache_read=int(getattr(getattr(cu, "prompt_tokens_details", None), "cached_tokens", 0) or 0),
            )
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(content)
                    yield TextDelta(content)
                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", None)
                    idx = idx if isinstance(idx, int) else len(calls)
                    entry = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        entry["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            entry["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            entry["args"] += fn.arguments
            fr = getattr(choice, "finish_reason", None)
            if fr:
                if fr == "error":
                    raise LLMBadResponse(detail="provider reported finish_reason=error mid-stream")
                finish = fr
    text = "".join(text_parts)
    tool_uses: list[ToolUse] = []
    for idx in sorted(calls):
        entry = calls[idx]
        raw = entry["args"] or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be a JSON object")
            tool_uses.append(ToolUse(id=entry["id"] or f"call_{idx}", name=entry["name"], input=parsed, raw_arguments=raw))
        except (json.JSONDecodeError, ValueError) as exc:
            tool_uses.append(ToolUse(id=entry["id"] or f"call_{idx}", name=entry["name"], input=None, raw_arguments=raw, parse_error=str(exc)))
    if not tool_uses:
        salvaged = _salvage_tool_call(text, tool_names)
        if salvaged:
            tool_uses.append(salvaged)
            text = ""
    stop_reason = FINISH_MAP.get(finish or "stop", "end_turn")
    if tool_uses and stop_reason == "end_turn":
        stop_reason = "tool_use"
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_uses:
        assistant_message["tool_calls"] = [
            {"id": tu.id, "type": "function", "function": {"name": tu.name, "arguments": tu.raw_arguments}} for tu in tool_uses
        ]
    for tu in tool_uses:
        yield tu
    yield MessageEnd(stop_reason=stop_reason, usage=usage, assistant_message=assistant_message, request_id=request_id, served_model=served_model)


def _salvage_tool_call(text: str, tool_names: set[str]) -> ToolUse | None:
    """Some open models write the tool call as JSON text instead of a tool_call block."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("name") not in tool_names:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    raw = json.dumps(args, ensure_ascii=False)
    log.info("salvaged tool call written as text: %s", obj["name"])
    return ToolUse(id=f"call_{uuid.uuid4().hex[:8]}", name=obj["name"], input=args, raw_arguments=raw, salvaged_from_text=True)


class OpenAICompatProvider:
    name = "openai-compatible"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "missing-api-key",
            timeout=Timeout(settings.llm_timeout_s, connect=settings.llm_connect_timeout_s),
            max_retries=0,
            default_headers={"HTTP-Referer": "https://github.com/anderson-matheus/dummy-insurance-co", "X-Title": "Dummy Insurance Claims Assistant"},
        )

    async def stream_turn(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        kwargs = build_request(req, self.settings)
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - translated at the seam
            err = translate_error(exc)
            log.warning("llm request failed: %s (%s)", err.code, err.detail)
            raise err from exc
        try:
            async for event in accumulate_chunks(stream, {t.name for t in req.tools}):
                yield event
        except Exception as exc:  # noqa: BLE001
            err = translate_error(exc)
            log.warning("llm stream failed: %s (%s)", err.code, err.detail)
            raise err from exc
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
