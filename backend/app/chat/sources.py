"""Numbered sources shared by the model, the citation validator and the UI."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.history.repo import Citation
from app.knowledge.claims_db import QueryResult
from app.knowledge.retriever import Chunk

MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
MAX_SOURCE_CHARS = 1400


@dataclass
class Source:
    n: int
    kind: str  # document | database
    chunk: Chunk | None = None
    query: QueryResult | None = None

    @property
    def key(self) -> str:
        if self.chunk is not None:
            return f"chunk:{self.chunk.id}"
        assert self.query is not None
        return "sql:" + re.sub(r"\s+", " ", self.query.sql.strip().lower())

    def label(self) -> str:
        if self.chunk is not None:
            c = self.chunk
            return f"{c.doc_code} v{c.version} · {c.section_label} · p. {c.page_start}"
        assert self.query is not None
        return f"Banco de sinistros · {self.query.row_count} linha(s)"

    def render_for_model(self) -> str:
        if self.chunk is not None:
            c = self.chunk
            status = "VIGENTE" if c.status == "vigente" else "SUPERADA (existe versão mais recente)"
            pages = f"p. {c.page_start}" if c.page_start == c.page_end else f"p. {c.page_start}-{c.page_end}"
            text = c.text if len(c.text) <= MAX_SOURCE_CHARS else c.text[:MAX_SOURCE_CHARS] + " (...)"
            return f"[{self.n}] {c.doc_code} v{c.version} — {c.doc_title} — {status}, vigência {c.effective_date} — {c.section_label} — {pages}\n{text}"
        assert self.query is not None
        q = self.query
        return f"[{self.n}] BANCO DE SINISTROS — consulta: {q.sql} — {q.snapshot}\n{q.as_text()}"

    def summary(self) -> dict:
        base = {"n": self.n, "source_type": self.kind, "label": self.label()}
        if self.chunk is not None:
            c = self.chunk
            base.update(doc_code=c.doc_code, doc_title=c.doc_title, doc_version=c.version, doc_status=c.status,
                        effective_date=c.effective_date, section=c.section_label, page=c.page_start)
        else:
            assert self.query is not None
            base.update(sql=self.query.sql, row_count=self.query.row_count, data_snapshot=self.query.snapshot)
        return base

    def to_citation(self) -> Citation:
        if self.chunk is not None:
            c = self.chunk
            return Citation(
                ordinal=self.n, source_type="document", doc_code=c.doc_code, doc_title=c.doc_title, doc_version=c.version,
                doc_status=c.status, section=c.section_label, page=c.page_start, chunk_id=c.id,
                snippet=_snippet(c.text),
            )
        assert self.query is not None
        q = self.query
        return Citation(
            ordinal=self.n, source_type="database", doc_title="Banco de sinistros", sql=q.sql, row_count=q.row_count,
            data_snapshot=q.snapshot, snippet=_snippet(q.as_text(max_rows=5)),
        )


def _snippet(text: str, limit: int = 400) -> str:
    body = text.split("\n", 1)[1] if "\n" in text else text
    body = body.strip()
    return body if len(body) <= limit else body[:limit].rstrip() + "…"


class SourceRegistry:
    def __init__(self) -> None:
        self._sources: list[Source] = []
        self._by_key: dict[str, Source] = {}

    def __len__(self) -> int:
        return len(self._sources)

    def all(self) -> list[Source]:
        return list(self._sources)

    def get(self, n: int) -> Source | None:
        return self._sources[n - 1] if 1 <= n <= len(self._sources) else None

    def _add(self, source: Source) -> tuple[Source, bool]:
        existing = self._by_key.get(source.key)
        if existing is not None:
            return existing, False
        source.n = len(self._sources) + 1
        self._sources.append(source)
        self._by_key[source.key] = source
        return source, True

    def add_chunk(self, chunk: Chunk) -> tuple[Source, bool]:
        return self._add(Source(n=0, kind="document", chunk=chunk))

    def add_query(self, result: QueryResult) -> tuple[Source, bool]:
        return self._add(Source(n=0, kind="database", query=result))

    def render_for_model(self, sources: list[Source] | None = None) -> str:
        return "\n\n".join(s.render_for_model() for s in (sources if sources is not None else self._sources))

    def summaries(self) -> list[dict]:
        return [s.summary() for s in self._sources]

    def validate_markers(self, text: str) -> tuple[str, list[Source], list[int]]:
        """Keep only markers that point to real sources; return cleaned text, cited sources, dropped numbers."""
        cited: dict[int, Source] = {}
        dropped: list[int] = []

        def repl(m: re.Match) -> str:
            numbers = [int(x) for x in re.split(r"\s*,\s*", m.group(1))]
            kept: list[int] = []
            for n in numbers:
                src = self.get(n)
                if src is None:
                    dropped.append(n)
                else:
                    cited.setdefault(n, src)
                    kept.append(n)
            return "".join(f"[{n}]" for n in kept)

        cleaned = MARKER_RE.sub(repl, text)
        cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        return cleaned, [cited[n] for n in sorted(cited)], dropped
