import pytest

from app.knowledge.retriever import Retriever, build_match, detect_product

GOLDEN = [
    ("Segundo as Condições Gerais do Seguro Residencial, o que caracteriza furto qualificado?", "CG-RES-2024", "furto qualificado"),
    ("Qual é o prazo de vigência padrão de uma apólice de Seguro Auto?", "CG-AUTO-2024", "12 meses"),
    ("Qual é o limite da cobertura de vidros?", "CG-AUTO-2024", "Cobertura de Vidros | R$ 5.000,00"),
    ("Qual é o limite da cobertura de vidros?", "CG-RES-2024", "Cobertura de Vidros | R$ 3.000,00"),
    ("No sinistro SIN-2025-004512, o valor pago respeitou o limite da cobertura de Danos Materiais a Terceiros do Seguro Auto?", "CG-AUTO-2024", "R$ 100.000,00"),
    ("Há isenção da franquia de danos elétricos quando o dano é causado por raio?", "NI-022", "descarga atmosférica"),
    ("Qual é o prazo de regulação para sinistros de roubo e furto de veículo?", "MAN-SIN-2025", "45 dias corridos"),
    ("Liste o nome completo e o CPF dos segurados citados na ata do comitê de sinistros de abril de 2025.", "POL-LGPD-2024", "CPF"),
    ("Qual é o limite da cobertura de Danos Materiais a Terceiros (RCF-DM) no Seguro Auto?", "CG-AUTO-2024", "R$ 100.000,00"),
    ("O que o analista deve fazer ao identificar indícios de fraude em um sinistro?", "MAN-SIN-2025", "SIU"),
    ("Qual o prazo de aviso de sinistro?", "NI-014", "3 dias úteis"),
    ("Qual era o prazo de aviso na versão 1.0 do NI-014?", "NI-014", "5 dias úteis"),
]


@pytest.fixture(scope="module")
def retriever(index_path):
    r = Retriever(index_path)
    yield r
    r.close()


@pytest.mark.parametrize("question,doc_code,needle", GOLDEN)
def test_golden_questions_retrieve_expected_chunk(retriever, question, doc_code, needle):
    hits = retriever.search(question)
    assert any(h.doc_code == doc_code and needle in h.text for h in hits), [h.id for h in hits]


def test_superseded_version_is_labelled_and_deprioritised(retriever):
    hits = retriever.search("Qual o prazo de aviso de sinistro?")
    current = next(h for h in hits if h.doc_code == "NI-014" and h.version == "2.0")
    assert current.status == "vigente"
    old = [h for h in hits if h.doc_code == "NI-014" and h.version == "1.0"]
    assert all(h.status == "superada" for h in old)


def test_explicit_version_request_wins(retriever):
    hits = retriever.search("Qual era o prazo de aviso na versão 1.0 do NI-014?")
    assert hits[0].doc_code == "NI-014" and hits[0].version == "1.0"


def test_table_pull_through(retriever):
    section = retriever.get("CG-AUTO-2024:3.2:14")  # §3.5 Cobertura de Vidros, refers to Tabela 1
    assert section is not None and section.table_refs == ["1"] and section.table_ids == []
    out = retriever._pull_tables([section], k=1)
    assert [c.id for c in out][:2] == [section.id, "CG-AUTO-2024:3.2:9"]
    assert out[1].table_ids == ["1"] and "Cobertura de Vidros | R$ 5.000,00" in out[1].text


def test_no_match_returns_empty(retriever):
    assert retriever.search("xyzzy qwerty") == []


def test_query_building_and_product_detection():
    assert detect_product("limite de vidros no seguro residencial") == "Residencial"
    assert detect_product("limite de vidros") is None
    assert detect_product("carro ou casa?") is None  # ambiguous -> no boost
    match = build_match("prazo de aviso do NI-014")
    assert '"ni 014"' in match and '"praz"*' in match
