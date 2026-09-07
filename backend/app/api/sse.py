"""Server-Sent Events helpers (fetch + ReadableStream on the client, since EventSource cannot POST)."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi.responses import StreamingResponse

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def format_sse(event: str, data: Any, event_id: int | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {payload}\n\n"


def sse_response(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(frames, media_type="text/event-stream", headers=SSE_HEADERS)
