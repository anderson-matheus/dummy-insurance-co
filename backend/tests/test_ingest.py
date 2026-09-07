import sqlite3
from datetime import date

from app.knowledge.ingest import DocInfo, assign_status, parse_document
from tests.conftest import CORPUS_DIR


def test_metadata_headers_and_sections():
    doc, chunks = parse_document(CORPUS_DIR / "01_cg-auto.pdf")
    assert (doc.code, doc.version, doc.effective_date, doc.product) == ("CG-AUTO-2024", "3.2", "2024-01-01", "Auto")
    assert doc.title == "Condições Gerais do Seguro Auto"
    joined = "\n".join(c.text for c in chunks)
    assert "USO INTERNO" not in joined and "Página" not in joined  # headers/footers stripped
    s11 = next(c for c in chunks if c.section_number == "1.1")
    assert "indicados na seção própria e na apólice" in s11.text  # paragraph rejoined across the page break
    table = next(c for c in chunks if c.table_ids == ["1"])
    assert "RCF-DM - Danos Materiais a Terceiros | R$ 100.000,00" in table.text
    assert "Cobertura de Vidros | R$ 5.000,00 | R$ 150,00" in table.text
    assert table.kind == "table" and table.page_start == 3
    vidros = next(c for c in chunks if c.section_number == "3.5")
    assert vidros.table_refs == ["1"]  # referenced table gets pulled at retrieval time


def test_footnotes_stay_with_table():
    _, chunks = parse_document(CORPUS_DIR / "08_ni-022-franquias.pdf")
    table = next(c for c in chunks if c.table_ids == ["1"])
    assert "Residencial | Danos Elétricos (3) | R$ 250,00" in table.text
    assert "(3) A franquia da cobertura de Danos Elétricos" in table.text
    assert "descarga atmosférica (raio)" in table.text


def test_two_column_faq_reading_order():
    _, chunks = parse_document(CORPUS_DIR / "09_faq-analistas.pdf")
    qa = [c for c in chunks if c.kind == "qa"]
    assert len(qa) >= 18
    merged = next(c for c in qa if "limite de danos a terceiros do auto?" in c.text)
    assert "linha RCF-DM" in merged.text


def test_version_supersession():
    docs = [
        DocInfo(code="NI-014", version="1.0", title="t", effective_date="2023-01-01", file="a", pages=2, sha256="x"),
        DocInfo(code="NI-014", version="2.0", title="t", effective_date="2025-06-01", file="b", pages=2, sha256="y"),
        DocInfo(code="NI-014", version="3.0", title="t", effective_date="2099-01-01", file="c", pages=2, sha256="z"),
        DocInfo(code="CG-AUTO-2024", version="3.2", title="t", effective_date="2024-01-01", file="d", pages=2, sha256="w"),
    ]
    assign_status(docs, today=date(2026, 9, 7))
    status = {(d.code, d.version): d.status for d in docs}
    assert status[("NI-014", "1.0")] == "superada"
    assert status[("NI-014", "2.0")] == "vigente"
    assert status[("NI-014", "3.0")] == "superada"  # not yet in force
    assert status[("CG-AUTO-2024", "3.2")] == "vigente"


def test_index_masks_pii_in_minutes(index_path):
    con = sqlite3.connect(index_path)
    text = con.execute("SELECT text FROM chunks WHERE doc_code = 'ATA-COM-2025-04' AND section_number = '4'").fetchone()[0]
    assert "Marta" not in text and "111.111.111-11" not in text and "90000-0001" not in text
    assert "SIN-2025-004512" in text  # non-identifying data is kept
    manifest = dict(con.execute("SELECT key, value FROM manifest").fetchall())
    assert manifest["documents"] == "13" and int(manifest["chunks"]) > 200
    statuses = dict(con.execute("SELECT version, status FROM documents WHERE doc_code = 'NI-014'").fetchall())
    assert statuses == {"1.0": "superada", "2.0": "vigente"}
