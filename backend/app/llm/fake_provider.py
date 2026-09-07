"""Scripted provider for tests and dry runs: no network, deterministic, failure injection."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from app.core.errors import LLMError
from app.llm.types import LLMEvent, LLMRequest, MessageEnd, TextDelta, ToolUse, Usage


@dataclass
class Scripted:
    events: list[LLMEvent]


@dataclass
class Fail:
    exc: LLMError
    after_text: str = ""  # emit this text first, then fail mid-stream


@dataclass
class Hang:
    seconds: float = 3600.0


Turn = Scripted | Fail | Hang


def _end(stop_reason: str, text: str, tool_uses: list[ToolUse] | None = None, usage: Usage | None = None) -> MessageEnd:
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_uses:
        msg["tool_calls"] = [
            {"id": tu.id, "type": "function", "function": {"name": tu.name, "arguments": tu.raw_arguments}} for tu in tool_uses
        ]
    return MessageEnd(stop_reason=stop_reason, usage=usage or Usage(120, 40), assistant_message=msg, request_id="fake-req", served_model="fake-model")


def answer(text: str, chunk: int = 12, usage: Usage | None = None) -> Scripted:
    events: list[LLMEvent] = [TextDelta(text[i : i + chunk]) for i in range(0, len(text), chunk)]
    events.append(_end("end_turn", text, usage=usage))
    return Scripted(events)


def tool_call(name: str, args: dict[str, Any], text: str = "", call_id: str = "call_1") -> Scripted:
    tu = ToolUse(id=call_id, name=name, input=args, raw_arguments=json.dumps(args, ensure_ascii=False))
    events: list[LLMEvent] = [TextDelta(text)] if text else []
    events += [tu, _end("tool_use", text, [tu])]
    return Scripted(events)


def malformed_tool_call(name: str, raw: str = "{not json", call_id: str = "call_x") -> Scripted:
    tu = ToolUse(id=call_id, name=name, input=None, raw_arguments=raw, parse_error="invalid json")
    return Scripted([tu, _end("tool_use", "", [tu])])


def refuse(reason_code: str = "NO_SOURCE", message: str = "Não há fonte para isso.") -> Scripted:
    return tool_call("refuse", {"reason_code": reason_code, "message": message})


def truncated(text: str) -> Scripted:
    return Scripted([TextDelta(text), _end("max_tokens", text)])


def provider_refusal() -> Scripted:
    return Scripted([_end("refusal", "")])


@dataclass
class FakeProvider:
    turns: list[Turn] = field(default_factory=list)
    router: Callable[[LLMRequest], Turn] | None = None
    name: str = "fake"
    calls: list[LLMRequest] = field(default_factory=list)

    def _next(self, req: LLMRequest) -> Turn:
        if self.router is not None:
            return self.router(req)
        if not self.turns:
            raise AssertionError("FakeProvider: no scripted turn left")
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]

    async def stream_turn(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        # snapshot: the orchestrator keeps mutating its message list after the call
        self.calls.append(LLMRequest(system=req.system, messages=[dict(m) for m in req.messages], tools=list(req.tools),
                                     max_tokens=req.max_tokens, tool_choice=req.tool_choice))
        turn = self._next(req)
        if isinstance(turn, Hang):
            await asyncio.sleep(turn.seconds)
            return
        if isinstance(turn, Fail):
            if turn.after_text:
                yield TextDelta(turn.after_text)
                await asyncio.sleep(0)
            raise turn.exc
        for ev in turn.events:
            yield ev
            await asyncio.sleep(0)
