"""Logging with a per-request correlation id (never exposed beyond the error envelope)."""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.cid = correlation_id.get()
        return True


def configure_logging(level: str = "info") -> None:
    root = logging.getLogger()
    if getattr(root, "_insurco_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(cid)s] %(name)s: %(message)s")
    )
    handler.addFilter(_CorrelationFilter())
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root._insurco_configured = True  # type: ignore[attr-defined]
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
