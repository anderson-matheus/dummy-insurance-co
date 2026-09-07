"""Lexical retrieval over the corpus index (SQLite FTS5 / BM25) with metadata boosts.

Why BM25 and not embeddings for the PoC: zero external calls, deterministic,
millisecond latency and full recall on the golden questions; the LLM can
reformulate queries through the ``search_documents`` tool. The ``Retriever``
interface is the seam where a hybrid (dense + lexical) engine plugs in for a
production corpus of tens of thousands of documents.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

import snowballstemmer

STEMMER = snowballstemmer.stemmer("portuguese")

STOPWORDS = set(
    """a o as os um uma uns umas de da do das dos em na no nas nos por para com sem sob sobre e ou mas
    que se ao aos à às pelo pela pelos pelas este esta isto esse essa isso aquele aquela aquilo seu sua seus suas
    meu minha nosso nossa ele ela eles elas eu tu nós vós lhe lhes me te nos vos ser é são foi era está estão
    estar ter tem têm há haver não sim mais menos muito pouco também já ainda como quando onde qual quais quem
    quanto quantos quanta quantas cujo cuja existe existem liste informe diga qual é o me mostre sobre
    the of and to in is""".split()
)
ACRONYMS = {"rcf", "dm", "dc", "siu", "cpf", "lgpd", "cg", "ni", "man", "sin", "faq", "pii", "app", "sac"}
SYNONYMS: dict[str, list[str]] = {
    "carro": ["veículo", "auto"],
    "carros": ["veículo", "auto"],
    "automóvel": ["veículo", "auto"],
    "veículo": ["auto"],
    "casa": ["residencial", "imóvel"],
    "imóvel": ["residencial"],
    "empresa": ["empresarial"],
    "raio": ["descarga atmosférica"],
    "terceiros": ["rcf"],
    "nome": ["dados pessoais"],
    "cpf": ["dados pessoais"],
    "telefone": ["dados pessoais"],
    "e-mail": ["dados pessoais"],
    "email": ["dados pessoais"],
    "comunicar": ["aviso"],
    "avisar": ["aviso"],
    "comunicação": ["aviso"],
    "aviso": ["comunicação", "comunicar"],
    "diárias": ["carro reserva"],
    "fraude": ["siu"],
    "alçada": ["alçadas"],
    "aprovar": ["alçadas"],
    "reajuste": ["sinistralidade"],
    "desconto": ["prêmio"],
    "pagou": ["pago"],
    "pagamento": ["pago"],
}
DOC_CODE_RE = re.compile(
    r"\b(NI-\d{3}|CG-(?:AUTO|RES|EMP)(?:-\d{4})?|MAN-(?:SIN|SUB)(?:-\d{4})?|POL-LGPD(?:-\d{4})?|COM-REAJ(?:-\d{4})?|FAQ-SIN(?:-\d{4})?|GLOS(?:-\d{4})?|ATA-COM(?:-\d{4}-\d{2})?)\b",
    re.I,
)
VERSION_RE = re.compile(r"vers[ãa]o\s*(\d+\.\d+)", re.I)
PRODUCT_WORDS = {
    "Auto": ["auto", "veiculo", "veiculos", "carro", "carros", "automovel"],
    "Residencial": ["residencial", "residencia", "casa", "imovel"],
    "Empresarial": ["empresarial", "empresa", "empresas"],
}


def normalize(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch))


def detect_product(question: str) -> str | None:
    words = set(re.findall(r"[a-z0-9]+", normalize(question)))
    hits = [p for p, ws in PRODUCT_WORDS.items() if words & set(ws)]
    return hits[0] if len(hits) == 1 else None


@dataclass
class Chunk:
    id: str
    doc_code: str
    doc_title: str
    version: str
    effective_date: str
    status: str  # vigente | superada
    product: str | None
    doc_type: str
    section_number: str
    section_title: str
    page_start: int
    page_end: int
    kind: str
    text: str
    table_ids: list[str]
    table_refs: list[str]
    score: float = 0.0

    @property
    def section_label(self) -> str:
        return f"§{self.section_number} {self.section_title}" if self.section_number != "0" else self.section_title


def _terms(question: str) -> tuple[list[str], list[str]]:
    """Return (FTS5 match terms, plain stems used for coverage scoring)."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", normalize(question)) if t not in STOPWORDS and len(t) >= 2]
    expanded: list[str] = []
    for raw_word in re.findall(r"[\wÀ-ÿ-]+", question.lower()):
        for syn in SYNONYMS.get(raw_word, []):
            expanded.extend(re.findall(r"[a-z0-9]+", normalize(syn)))
    terms: list[str] = []
    stems: list[str] = []
    seen: set[str] = set()
    for tok in tokens + expanded:
        if tok in seen:
            continue
        seen.add(tok)
        if tok in ACRONYMS or len(tok) <= 3 or tok.isdigit():
            terms.append(f'"{tok}"')
            stems.append(tok)
            continue
        stem = STEMMER.stemWord(tok)
        if len(stem) >= 4 and stem != tok:
            terms.append(f'"{stem}"*')
            stems.append(stem)
        else:
            terms.append(f'"{tok}"')
            stems.append(tok)
    return terms, stems


def build_match(question: str) -> str:
    terms, _ = _terms(question)
    for code in DOC_CODE_RE.findall(question):
        parts = re.findall(r"[a-z0-9]+", normalize(code if isinstance(code, str) else code[0]))
        if parts:
            terms.append('"' + " ".join(parts) + '"')
    return " OR ".join(terms) if terms else '""'


class Retriever:
    def __init__(self, index_path: str) -> None:
        self.index_path = index_path
        self._con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, check_same_thread=False)
        self._con.row_factory = sqlite3.Row

    def close(self) -> None:
        self._con.close()

    def info(self) -> dict:
        m = dict(self._con.execute("SELECT key, value FROM manifest").fetchall())
        return {"built_at": m.get("built_at"), "documents": int(m.get("documents", 0)), "chunks": int(m.get("chunks", 0))}

    def documents(self) -> list[dict]:
        return [dict(r) for r in self._con.execute("SELECT * FROM documents ORDER BY doc_code, version")]

    def get(self, chunk_id: str) -> Chunk | None:
        row = self._con.execute(
            "SELECT c.*, d.title AS doc_title, d.effective_date, d.status, d.product, d.doc_type "
            "FROM chunks c JOIN documents d ON d.doc_code = c.doc_code AND d.version = c.version WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        return self._row(row) if row else None

    def search(self, question: str, k: int = 8, product: str | None = None) -> list[Chunk]:
        match = build_match(question)
        product = product or detect_product(question)
        wanted_codes = {normalize(c).upper() for c in DOC_CODE_RE.findall(question)}
        wanted_version = (VERSION_RE.search(question) or [None, None])[1]
        _, stems = _terms(question)
        try:
            rows = self._con.execute(
                """
                SELECT c.*, d.title AS doc_title, d.effective_date, d.status, d.product, d.doc_type,
                       bm25(chunks_fts, 0, 3.0, 4.0, 1.0) AS score
                FROM chunks_fts f
                JOIN chunks c ON c.id = f.chunk_id
                JOIN documents d ON d.doc_code = c.doc_code AND d.version = c.version
                WHERE chunks_fts MATCH ?
                ORDER BY score LIMIT 200
                """,
                (match,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        chunks = [self._row(r) for r in rows]
        for ch in chunks:
            ch.score = ch.score * self._boost(ch, stems, wanted_codes, wanted_version, product)
        chunks.sort(key=lambda c: c.score)
        top = chunks[:k]
        return self._pull_tables(top, k)

    @staticmethod
    def _boost(ch: Chunk, stems: list[str], wanted_codes: set[str], wanted_version: str | None, product: str | None) -> float:
        """Multiplicative boost on the (negative) bm25 score: >1 means better.

        Coverage (share of query terms present) counters BM25's preference for
        very short chunks; section-title hits reward the section that is about
        the question; metadata boosts handle explicit doc codes/versions,
        superseded versions and the product named in the question.
        """
        body_tokens = re.findall(r"[a-z0-9]+", normalize(ch.text))
        title_tokens = re.findall(r"[a-z0-9]+", normalize(ch.section_title))
        matched = sum(1 for st in stems if any(t.startswith(st) for t in body_tokens))
        title_hits = sum(1 for st in stems if any(t.startswith(st) for t in title_tokens))
        coverage = matched / len(stems) if stems else 1.0
        boost = (0.5 + coverage) * (1 + 0.35 * min(title_hits, 3))
        if wanted_codes and any(ch.doc_code.upper().startswith(w) for w in wanted_codes):
            boost *= 2.5
        if wanted_version:
            boost *= 2.0 if ch.version == wanted_version else 0.9
        elif ch.status == "superada":
            boost *= 0.75
        if product and ch.product:
            boost *= 1.1 if ch.product == product else 0.9
        return boost

    def _pull_tables(self, top: list[Chunk], k: int) -> list[Chunk]:
        """Insert the chunk holding a referenced table right after the chunk that references it."""
        out: list[Chunk] = []
        seen = {c.id for c in top}
        for ch in top:
            out.append(ch)
            for ref in ch.table_refs:
                row = self._con.execute(
                    "SELECT c.*, d.title AS doc_title, d.effective_date, d.status, d.product, d.doc_type "
                    "FROM chunks c JOIN documents d ON d.doc_code = c.doc_code AND d.version = c.version "
                    "WHERE c.doc_code = ? AND c.version = ? AND (',' || c.table_ids || ',') LIKE ? LIMIT 1",
                    (ch.doc_code, ch.version, f"%,{ref},%"),
                ).fetchone()
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    pulled = self._row(row)
                    pulled.score = ch.score
                    out.append(pulled)
        return out[: k + 2]

    @staticmethod
    def _row(r: sqlite3.Row) -> Chunk:
        return Chunk(
            id=r["id"], doc_code=r["doc_code"], doc_title=r["doc_title"], version=r["version"],
            effective_date=r["effective_date"], status=r["status"], product=r["product"], doc_type=r["doc_type"],
            section_number=r["section_number"], section_title=r["section_title"], page_start=r["page_start"],
            page_end=r["page_end"], kind=r["kind"], text=r["text"],
            table_ids=[t for t in (r["table_ids"] or "").split(",") if t],
            table_refs=[t for t in (r["table_refs"] or "").split(",") if t],
            score=float(r["score"]) if "score" in r.keys() else 0.0,
        )
