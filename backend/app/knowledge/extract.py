"""PDF text extraction with PyMuPDF: column-aware block ordering and table capture.

The corpus PDFs repeat headers/footers on every page, use numbered section
headings, contain ruled tables (captured with ``page.find_tables``) and one
two-column FAQ. This module turns each page into an ordered list of blocks
(paragraph, heading, table) with page geometry, leaving semantic decisions to
``ingest.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

HEADER_RE = re.compile(r"^(Indicium InsurCo\b|USO INTERNO - documento controlado\b)")
HEADER_RIGHT_RE = re.compile(r"^(\S+ v\S+|Vigência: \d{4}-\d{2}-\d{2})$")
FOOTER_RE = re.compile(r"(Indicium InsurCo - Uso interno|^Página \d+ de \d+$)")
LIST_LINE_RE = re.compile(r"^\s*(-\s|\(\d\)\s|•\s)")


@dataclass
class Block:
    page: int  # 1-based
    y0: float
    text: str
    kind: str = "text"  # text | table
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class PageText:
    number: int
    width: float
    height: float
    blocks: list[Block]


def _join_lines(lines: list[str]) -> str:
    """Join wrapped lines with spaces; keep list items and footnotes on their own line."""
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out and LIST_LINE_RE.match(line):
            out.append("\n" + line)
        elif out:
            out.append(" " + line)
        else:
            out.append(line)
    return "".join(out)


def _is_header_or_footer(text: str, y0: float, y1: float, height: float) -> bool:
    if y1 < 60 and (HEADER_RE.match(text) or HEADER_RIGHT_RE.match(text)):
        return True
    if y0 > height - 50 and FOOTER_RE.search(text):
        return True
    return False


def _order_blocks(raw: list[tuple[float, float, float, float, object]], width: float) -> list[object]:
    """Reading order for one- and two-column pages.

    Consecutive blocks that do not span the page middle form a band; inside a
    band the left column is emitted before the right column. Returns payloads.
    """
    mid = width / 2
    raw = sorted(raw, key=lambda b: (b[1], b[0]))
    ordered: list[object] = []
    band: list[tuple[float, float, float, float, object]] = []

    def flush() -> None:
        left = sorted((b for b in band if b[2] <= mid + 5), key=lambda b: b[1])
        right = sorted((b for b in band if b[2] > mid + 5), key=lambda b: b[1])
        ordered.extend(b[4] for b in left + right)
        band.clear()

    for b in raw:
        x0, _y0, x1, _y1, payload = b
        if x0 < mid and x1 > mid + 5:
            flush()
            ordered.append(payload)
        else:
            band.append(b)
    flush()
    return ordered


def extract_pages(pdf_path: str | Path) -> tuple[list[PageText], dict[str, str]]:
    """Return ordered blocks per page plus the PDF metadata dict."""
    doc = pymupdf.open(str(pdf_path))
    pages: list[PageText] = []
    for pno, page in enumerate(doc, start=1):
        width, height = page.rect.width, page.rect.height
        tables = page.find_tables()
        table_boxes = [t.bbox for t in tables.tables]
        raw_blocks: list[tuple[float, float, float, float, Block]] = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            x0, y0, x1, y1 = b["bbox"]
            lines = ["".join(s["text"] for s in ln["spans"]) for ln in b["lines"]]
            text = _join_lines(lines)
            if not text or _is_header_or_footer(text, y0, y1, height):
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if any(bx0 <= cx <= bx1 and by0 <= cy <= by1 for (bx0, by0, bx1, by1) in table_boxes):
                continue  # rendered from the table object instead
            raw_blocks.append((x0, y0, x1, y1, Block(page=pno, y0=y0, text=text)))
        for t in tables.tables:
            rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in t.extract()]
            rows = [r for r in rows if any(r)]
            text = "\n".join(" | ".join(r) for r in rows)
            bx0, by0, bx1, by1 = t.bbox
            raw_blocks.append((bx0, by0, bx1, by1, Block(page=pno, y0=by0, text=text, kind="table", rows=rows)))
        blocks = _order_blocks(raw_blocks, width)
        pages.append(PageText(number=pno, width=width, height=height, blocks=blocks))
    meta = {k: (v or "") for k, v in (doc.metadata or {}).items()}
    doc.close()
    return pages, meta
