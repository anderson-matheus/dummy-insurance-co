"""Conversation history persistence (SQLite via aiosqlite).

Modeling summary (see DECISIONS.md):
- one row per message; the assistant reply points to its user message via
  ``parent_message_id`` (unique), so a retry updates the same reply row in
  place (``attempt`` += 1) and history never shows duplicates;
- ``client_message_id`` is unique per conversation and gives idempotent POSTs;
- per-message ``status`` + ``error_code`` let the UI render error/refusal/
  cancelled/interrupted states after a reload;
- citations and metrics live in their own tables so evaluations and the
  provenance footer can query them directly.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

TERMINAL_STATUSES = {"complete", "refused", "error", "cancelled", "interrupted"}
RETRYABLE_STATUSES = {"error", "cancelled", "interrupted"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Citation:
    ordinal: int
    source_type: str  # document | database
    doc_code: str | None = None
    doc_title: str | None = None
    doc_version: str | None = None
    doc_status: str | None = None
    section: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    snippet: str | None = None
    sql: str | None = None
    row_count: int | None = None
    data_snapshot: str | None = None


@dataclass
class Metrics:
    model: str | None = None
    served_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    llm_calls: int = 0
    retries: int = 0
    tool_calls: int = 0
    pii_hits: int = 0
    provider_request_ids: list[str] = field(default_factory=list)


@dataclass
class Message:
    id: str
    conversation_id: str
    seq: int
    role: str
    content: str
    status: str
    client_message_id: str | None
    parent_message_id: str | None
    attempt: int
    error_code: str | None
    refusal_code: str | None
    provenance: dict[str, Any] | None
    created_at: str
    completed_at: str | None
    citations: list[Citation] = field(default_factory=list)
    metrics: Metrics | None = None


@dataclass
class Conversation:
    id: str
    title: str | None
    created_at: str
    updated_at: str
    message_count: int = 0


class HistoryRepo:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "HistoryRepo not connected"
        return self._db

    # -------------------------------------------------------------- conversations
    async def create_conversation(self, title: str | None = None) -> Conversation:
        now = utcnow()
        cid = new_id()
        await self.db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
        await self.db.commit()
        return Conversation(id=cid, title=title, created_at=now, updated_at=now)

    async def list_conversations(self) -> list[Conversation]:
        rows = await self.db.execute_fetchall(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            ORDER BY c.updated_at DESC
            """
        )
        return [Conversation(**dict(r)) for r in rows]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = await self._fetchone(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c WHERE c.id = ?
            """,
            (conversation_id,),
        )
        return Conversation(**dict(row)) if row else None

    async def delete_conversation(self, conversation_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def _touch(self, conversation_id: str, title_candidate: str | None = None) -> None:
        await self.db.execute(
            "UPDATE conversations SET updated_at = ?, title = COALESCE(title, ?) WHERE id = ?",
            (utcnow(), title_candidate, conversation_id),
        )

    # ------------------------------------------------------------------- messages
    async def get_messages(self, conversation_id: str) -> list[Message]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (conversation_id,)
        )
        messages = [self._row_to_message(r) for r in rows]
        await self._attach_details(messages)
        return messages

    async def get_message(self, message_id: str) -> Message | None:
        row = await self._fetchone("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not row:
            return None
        msg = self._row_to_message(row)
        await self._attach_details([msg])
        return msg

    async def find_user_message(self, conversation_id: str, client_message_id: str) -> Message | None:
        row = await self._fetchone(
            "SELECT * FROM messages WHERE conversation_id = ? AND client_message_id = ? AND role = 'user'",
            (conversation_id, client_message_id),
        )
        return self._row_to_message(row) if row else None

    async def get_reply(self, parent_message_id: str) -> Message | None:
        row = await self._fetchone(
            "SELECT * FROM messages WHERE parent_message_id = ? AND role = 'assistant'",
            (parent_message_id,),
        )
        if not row:
            return None
        msg = self._row_to_message(row)
        await self._attach_details([msg])
        return msg

    async def add_user_message(
        self, conversation_id: str, content: str, client_message_id: str | None
    ) -> Message:
        mid = new_id()
        now = utcnow()
        await self.db.execute(
            """
            INSERT INTO messages (id, conversation_id, seq, role, content, status, client_message_id, created_at, completed_at)
            SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, 'user', ?, 'complete', ?, ?, ?
            FROM messages WHERE conversation_id = ?
            """,
            (mid, conversation_id, content, client_message_id, now, now, conversation_id),
        )
        await self._touch(conversation_id, content.strip()[:80] or None)
        await self.db.commit()
        return await self.get_message(mid)  # type: ignore[return-value]

    async def add_pending_reply(self, conversation_id: str, parent_message_id: str) -> Message:
        mid = new_id()
        now = utcnow()
        await self.db.execute(
            """
            INSERT INTO messages (id, conversation_id, seq, role, content, status, parent_message_id, created_at)
            SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, 'assistant', '', 'pending', ?, ?
            FROM messages WHERE conversation_id = ?
            """,
            (mid, conversation_id, parent_message_id, now, conversation_id),
        )
        await self.db.commit()
        return await self.get_message(mid)  # type: ignore[return-value]

    async def reset_reply_for_retry(self, message_id: str) -> Message:
        """Regenerate in place: bump attempt, clear content/error/citations/metrics."""
        await self.db.execute("DELETE FROM citations WHERE message_id = ?", (message_id,))
        await self.db.execute("DELETE FROM message_metrics WHERE message_id = ?", (message_id,))
        await self.db.execute(
            """
            UPDATE messages SET attempt = attempt + 1, content = '', status = 'pending',
                   error_code = NULL, refusal_code = NULL, provenance = NULL,
                   completed_at = NULL, created_at = ?
            WHERE id = ?
            """,
            (utcnow(), message_id),
        )
        await self.db.commit()
        return await self.get_message(message_id)  # type: ignore[return-value]

    async def finalize_reply(
        self,
        message_id: str,
        *,
        content: str,
        status: str,
        error_code: str | None = None,
        refusal_code: str | None = None,
        citations: Iterable[Citation] = (),
        metrics: Metrics | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Message:
        assert status in TERMINAL_STATUSES, status
        now = utcnow()
        await self.db.execute(
            """
            UPDATE messages SET content = ?, status = ?, error_code = ?, refusal_code = ?,
                   provenance = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                content,
                status,
                error_code,
                refusal_code,
                json.dumps(provenance, ensure_ascii=False) if provenance else None,
                now,
                message_id,
            ),
        )
        await self.db.execute("DELETE FROM citations WHERE message_id = ?", (message_id,))
        for c in citations:
            await self.db.execute(
                """
                INSERT INTO citations (message_id, ordinal, source_type, doc_code, doc_title, doc_version,
                    doc_status, section, page, chunk_id, snippet, sql, row_count, data_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, c.ordinal, c.source_type, c.doc_code, c.doc_title, c.doc_version,
                    c.doc_status, c.section, c.page, c.chunk_id, c.snippet, c.sql, c.row_count,
                    c.data_snapshot,
                ),
            )
        await self.db.execute("DELETE FROM message_metrics WHERE message_id = ?", (message_id,))
        if metrics is not None:
            await self.db.execute(
                """
                INSERT INTO message_metrics (message_id, model, served_model, input_tokens, output_tokens,
                    cost_usd, latency_ms, llm_calls, retries, tool_calls, pii_hits, provider_request_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, metrics.model, metrics.served_model, metrics.input_tokens,
                    metrics.output_tokens, metrics.cost_usd, metrics.latency_ms, metrics.llm_calls,
                    metrics.retries, metrics.tool_calls, metrics.pii_hits,
                    json.dumps(metrics.provider_request_ids),
                ),
            )
        row = await self._fetchone("SELECT conversation_id FROM messages WHERE id = ?", (message_id,))
        if row:
            await self._touch(row["conversation_id"])
        await self.db.commit()
        return await self.get_message(message_id)  # type: ignore[return-value]

    async def sweep_pending(self, error_code: str = "SERVER_RESTART") -> int:
        """Mark replies left 'pending' by a crash/restart as interrupted."""
        cur = await self.db.execute(
            "UPDATE messages SET status = 'interrupted', error_code = ?, completed_at = ? WHERE status = 'pending'",
            (error_code, utcnow()),
        )
        await self.db.commit()
        return cur.rowcount

    async def recent_turns(self, conversation_id: str, before_seq: int, limit: int) -> list[Message]:
        """Last ``limit`` completed user/assistant messages before ``before_seq`` (oldest first)."""
        rows = await self.db.execute_fetchall(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND seq < ? AND status IN ('complete', 'refused')
            ORDER BY seq DESC LIMIT ?
            """,
            (conversation_id, before_seq, limit),
        )
        return [self._row_to_message(r) for r in reversed(list(rows))]

    # -------------------------------------------------------------------- helpers
    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cur = await self.db.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    @staticmethod
    def _row_to_message(r: aiosqlite.Row) -> Message:
        d = dict(r)
        prov = d.pop("provenance", None)
        return Message(
            **d,
            provenance=json.loads(prov) if prov else None,
        )

    async def _attach_details(self, messages: list[Message]) -> None:
        by_id = {m.id: m for m in messages if m.role == "assistant"}
        if not by_id:
            return
        placeholders = ",".join("?" * len(by_id))
        ids = tuple(by_id)
        crows = await self.db.execute_fetchall(
            f"SELECT * FROM citations WHERE message_id IN ({placeholders}) ORDER BY message_id, ordinal",
            ids,
        )
        for r in crows:
            d = dict(r)
            mid = d.pop("message_id")
            d.pop("id")
            by_id[mid].citations.append(Citation(**d))
        mrows = await self.db.execute_fetchall(
            f"SELECT * FROM message_metrics WHERE message_id IN ({placeholders})", ids
        )
        for r in mrows:
            d = dict(r)
            mid = d.pop("message_id")
            d["provider_request_ids"] = json.loads(d.get("provider_request_ids") or "[]")
            by_id[mid].metrics = Metrics(**d)
