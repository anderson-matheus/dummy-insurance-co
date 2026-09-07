"""Corpus ingestion: PDF -> section-aware chunks -> SQLite (metadata + FTS5).

Run ``python -m app.knowledge.ingest`` (idempotent; ``--force`` rebuilds).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.knowledge.extract import Block, extract_pages
from app.knowledge.pii import PIIScrubber, load_policyholder_names

META_RE = re.compile(
    r"Código do documento:\s*(?P<code>\S+)\s+Versão:\s*(?P<version>\S+)\s+Vigência a partir de:\s*(?P<eff>\d{4}-\d{2}-\d{2})"
)
HEADING_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\s+(?P<title>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\n]{2,90})$")
TABLE_CAPTION_RE = re.compile(r"^Tabela (?P<n>\d+) - (?P<caption>.+)$")
TABLE_REF_RE = re.compile(r"\bTabela (\d+)\b")
FOOTNOTE_RE = re.compile(r"^\(\d\)\s")
QA_RE = re.compile(r"^Pergunta:")
SENTENCE_END = re.compile(r"[.:;!?]$")

MAX_CHUNK_CHARS = 1200
TARGET_PIECE_CHARS = 900
TABLE_SECTION_MAX = 1700

PRODUCT_BY_WORD = {"Seguro Auto": "Auto", "Residencial": "Residencial", "Empresarial": "Empresarial"}
DOC_TYPE_BY_PREFIX = [
    ("CG-", "CG"), ("NI-", "NI"), ("MAN-SIN", "MAN"), ("MAN-SUB", "MAN"), ("FAQ", "FAQ"),
    ("ATA", "ATA"), ("POL", "POL"), ("COM", "COM"), ("GLOS", "GLOS"),
]


@dataclass
class DocInfo:
    code: str
    version: str
    title: str
    effective_date: str
    file: str
    pages: int
    sha256: str
    product: str | None = None
    doc_type: str = "OTHER"
    status: str = "vigente"


@dataclass
class Chunk:
    doc: DocInfo
    ordinal: int
    section_number: str
    section_title: str
    page_start: int
    page_end: int
    kind: str  # section | table | qa
    text: str
    table_ids: list[str] = field(default_factory=list)
    table_refs: list[str] = field(default_factory=list)
    pii_masked: bool = False

    @property
    def id(self) -> str:
        return f"{self.doc.code}:{self.doc.version}:{self.ordinal}"


# ----------------------------------------------------------------------------- parsing
def _doc_type(code: str) -> str:
    for prefix, kind in DOC_TYPE_BY_PREFIX:
        if code.startswith(prefix):
            return kind
    return "OTHER"


def _product(title: str) -> str | None:
    for word, product in PRODUCT_BY_WORD.items():
        if word in title:
            return product
    return None


@dataclass
class Unit:
    """A paragraph-level unit with page span (may be a table with its caption)."""

    text: str
    page_start: int
    page_end: int
    kind: str = "text"  # text | table | footnote | caption


def _flatten(pages) -> tuple[list[Unit], str | None, dict | None]:
    """Flatten blocks into units, extract title/metadata, rejoin cross-page paragraphs."""
    units: list[Unit] = []
    title: str | None = None
    meta: dict | None = None
    prev_block: Block | None = None
    for page in pages:
        first_on_page = True
        for b in page.blocks:
            text = b.text.strip()
            if not text:
                continue
            if meta is None:
                m = META_RE.search(text)
                if m:
                    meta = m.groupdict()
                    if prev_block is not None and title is None:
                        title = prev_block.text.strip()
                    prev_block = b
                    continue
                if page.number == 1 and title is None:
                    prev_block = b
                    continue  # title candidate (the block right before metadata)
            kind = "table" if b.kind == "table" else "text"
            if kind == "text" and FOOTNOTE_RE.match(text):
                kind = "footnote"
            elif kind == "text" and TABLE_CAPTION_RE.match(text):
                kind = "caption"
            # rejoin a paragraph split by a page break
            if (
                first_on_page
                and units
                and kind == "text"
                and units[-1].kind == "text"
                and not SENTENCE_END.search(units[-1].text)
                and (text[0].islower() or text[0] == "(")
                and not HEADING_RE.match(text)
            ):
                units[-1].text += " " + text
                units[-1].page_end = page.number
            else:
                units.append(Unit(text=text, page_start=page.number, page_end=page.number, kind=kind))
            first_on_page = False
            prev_block = b
    # attach captions to the table that follows them
    merged: list[Unit] = []
    i = 0
    while i < len(units):
        u = units[i]
        if u.kind == "caption" and i + 1 < len(units) and units[i + 1].kind == "table":
            t = units[i + 1]
            merged.append(Unit(text=u.text + "\n" + t.text, page_start=u.page_start, page_end=t.page_end, kind="table"))
            i += 2
            continue
        # orphan short line (e.g. glossary term) -> merge with next paragraph
        if (
            u.kind == "text"
            and len(u.text) < 40
            and not SENTENCE_END.search(u.text)
            and not HEADING_RE.match(u.text)
            and i + 1 < len(units)
            and units[i + 1].kind == "text"
            and not HEADING_RE.match(units[i + 1].text)
        ):
            nxt = units[i + 1]
            merged.append(Unit(text=u.text + ": " + nxt.text, page_start=u.page_start, page_end=nxt.page_end, kind="text"))
            i += 2
            continue
        merged.append(u)
        i += 1
    return merged, title, meta


@dataclass
class Section:
    number: str
    title: str
    units: list[Unit]


def _sections(units: list[Unit]) -> list[Section]:
    sections: list[Section] = []
    current = Section(number="0", title="Preâmbulo", units=[])
    for u in units:
        m = HEADING_RE.match(u.text) if u.kind == "text" else None
        if m:
            if current.units:
                sections.append(current)
            current = Section(number=m.group("num"), title=m.group("title").strip(), units=[])
        else:
            current.units.append(u)
    if current.units:
        sections.append(current)
    return sections


def _pieces(units: list[Unit]) -> list[list[Unit]]:
    """Greedy split of a long section into ~TARGET_PIECE_CHARS pieces at unit boundaries."""
    pieces: list[list[Unit]] = []
    cur: list[Unit] = []
    size = 0
    for u in units:
        if cur and size + len(u.text) > TARGET_PIECE_CHARS:
            pieces.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += len(u.text)
    if cur:
        pieces.append(cur)
    return pieces


def _qa_units(units: list[Unit]) -> list[Unit]:
    """Merge a 'Pergunta:' block without 'Resposta:' with its continuation."""
    out: list[Unit] = []
    for u in units:
        if out and QA_RE.match(out[-1].text) and "Resposta:" not in out[-1].text and not QA_RE.match(u.text):
            out[-1].text += " " + u.text
            out[-1].page_end = u.page_end
        else:
            out.append(u)
    return out


def build_chunks(doc: DocInfo, units: list[Unit]) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0

    def emit(section: Section, group: list[Unit], kind: str, part: int | None = None) -> None:
        nonlocal ordinal
        ordinal += 1
        heading = f"{section.number} {section.title}" if section.number != "0" else section.title
        if part is not None:
            heading += f" (parte {part})"
        body = "\n".join(u.text for u in group)
        text = f"{heading}\n{body}"
        table_ids = [m.group("n") for u in group if u.kind == "table" for m in [TABLE_CAPTION_RE.match(u.text.split("\n", 1)[0])] if m]
        refs = sorted({r for r in TABLE_REF_RE.findall(body) if r not in table_ids})
        chunks.append(
            Chunk(
                doc=doc, ordinal=ordinal, section_number=section.number, section_title=section.title,
                page_start=min(u.page_start for u in group), page_end=max(u.page_end for u in group),
                kind=kind, text=text, table_ids=table_ids, table_refs=refs,
            )
        )

    for section in _sections(units):
        if any(QA_RE.match(u.text) for u in section.units):
            for u in _qa_units(section.units):
                emit(section, [u], "qa" if QA_RE.match(u.text) else "section")
            continue
        total = sum(len(u.text) for u in section.units)
        has_table = any(u.kind == "table" for u in section.units)
        limit = TABLE_SECTION_MAX if has_table else MAX_CHUNK_CHARS
        if total <= limit:
            emit(section, section.units, "table" if has_table else "section")
            continue
        pieces = _pieces(section.units)
        for i, group in enumerate(pieces, start=1):
            kind = "table" if any(u.kind == "table" for u in group) else "section"
            emit(section, group, kind, part=i if len(pieces) > 1 else None)
    return chunks


def parse_document(pdf_path: Path) -> tuple[DocInfo, list[Chunk]]:
    pages, _meta = extract_pages(pdf_path)
    units, title, meta = _flatten(pages)
    if not meta:
        raise ValueError(f"{pdf_path.name}: metadata line not found")
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    doc = DocInfo(
        code=meta["code"], version=meta["version"], title=title or pdf_path.stem,
        effective_date=meta["eff"], file=pdf_path.name, pages=len(pages), sha256=sha,
    )
    doc.product = _product(doc.title)
    doc.doc_type = _doc_type(doc.code)
    return doc, build_chunks(doc, units)


def assign_status(docs: list[DocInfo], today: date | None = None) -> None:
    today = today or date.today()
    by_code: dict[str, list[DocInfo]] = {}
    for d in docs:
        by_code.setdefault(d.code, []).append(d)
    for versions in by_code.values():
        in_force = [d for d in versions if date.fromisoformat(d.effective_date) <= today]
        current = max(in_force or versions, key=lambda d: d.effective_date)
        for d in versions:
            d.status = "vigente" if d is current else "superada"


# ----------------------------------------------------------------------------- storage
INDEX_SCHEMA = """
CREATE TABLE documents (
    doc_code TEXT NOT NULL, version TEXT NOT NULL, title TEXT NOT NULL, doc_type TEXT, product TEXT,
    effective_date TEXT NOT NULL, status TEXT NOT NULL, file TEXT NOT NULL, pages INTEGER, sha256 TEXT,
    PRIMARY KEY (doc_code, version)
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY, doc_code TEXT NOT NULL, version TEXT NOT NULL, ordinal INTEGER NOT NULL,
    section_number TEXT, section_title TEXT, page_start INTEGER, page_end INTEGER, kind TEXT,
    table_ids TEXT, table_refs TEXT, text TEXT NOT NULL, pii_masked INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED, doc_title, section_title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT);
"""


def corpus_hash(pdfs: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(pdfs):
        h.update(p.name.encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def build_index(corpus_dir: str, index_path: str, claims_db_path: str | None = None, force: bool = False, log=print) -> dict:
    pdfs = sorted(Path(corpus_dir).glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"no PDFs in {corpus_dir}")
    chash = corpus_hash(pdfs)
    out = Path(index_path)
    if out.exists() and not force:
        con = sqlite3.connect(out)
        try:
            row = con.execute("SELECT value FROM manifest WHERE key = 'corpus_hash'").fetchone()
        except sqlite3.Error:
            row = None
        con.close()
        if row and row[0] == chash:
            log(f"index up to date ({out})")
            return read_manifest(index_path)
    names = load_policyholder_names(claims_db_path) if claims_db_path and Path(claims_db_path).exists() else []
    scrubber = PIIScrubber(names)
    docs: list[DocInfo] = []
    all_chunks: list[Chunk] = []
    for pdf in pdfs:
        doc, chunks = parse_document(pdf)
        for c in chunks:
            masked, hits = scrubber.scrub(c.text)
            if hits:
                c.text, c.pii_masked = masked, True
        docs.append(doc)
        all_chunks.extend(chunks)
        log(f"{pdf.name}: {doc.code} v{doc.version} ({doc.effective_date}) -> {len(chunks)} chunks")
    assign_status(docs)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(INDEX_SCHEMA)
    for d in docs:
        con.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.code, d.version, d.title, d.doc_type, d.product, d.effective_date, d.status, d.file, d.pages, d.sha256),
        )
    for c in all_chunks:
        con.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.id, c.doc.code, c.doc.version, c.ordinal, c.section_number, c.section_title, c.page_start,
                c.page_end, c.kind, ",".join(c.table_ids), ",".join(c.table_refs), c.text, int(c.pii_masked),
            ),
        )
        con.execute(
            "INSERT INTO chunks_fts (chunk_id, doc_title, section_title, body) VALUES (?,?,?,?)",
            (c.id, c.doc.title, c.section_title, c.text),
        )
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "corpus_hash": chash,
        "documents": str(len(docs)),
        "chunks": str(len(all_chunks)),
        "pii_masked_chunks": str(sum(1 for c in all_chunks if c.pii_masked)),
    }
    con.executemany("INSERT INTO manifest VALUES (?, ?)", manifest.items())
    con.commit()
    con.close()
    if out.exists():
        out.unlink()
    tmp.rename(out)
    log(f"index built: {len(docs)} documents, {len(all_chunks)} chunks -> {out}")
    return manifest


def read_manifest(index_path: str) -> dict:
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM manifest").fetchall())
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    from app.core.config import get_settings

    s = get_settings()
    ap = argparse.ArgumentParser(description="Build the corpus index (SQLite + FTS5)")
    ap.add_argument("--corpus", default=s.corpus_dir)
    ap.add_argument("--index", default=s.index_db_path)
    ap.add_argument("--claims-db", default=s.claims_db_path)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dump", action="store_true", help="print chunks instead of building")
    args = ap.parse_args(argv)
    if args.dump:
        for pdf in sorted(Path(args.corpus).glob("*.pdf")):
            doc, chunks = parse_document(pdf)
            for c in chunks:
                print(f"--- {c.id} [{c.kind}] §{c.section_number} p.{c.page_start}-{c.page_end} refs={c.table_refs} tables={c.table_ids}\n{c.text}\n")
        return 0
    build_index(args.corpus, args.index, args.claims_db, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
