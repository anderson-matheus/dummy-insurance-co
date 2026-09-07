from app.knowledge.pii import MASK, PIIScrubber


def test_patterns_are_masked():
    s = PIIScrubber()
    text, hits = s.scrub("CPF 111.111.111-11, telefone (41) 90000-0001, e-mail marta.b@exemplo-ficticio.com.br")
    assert hits == 3
    assert "111.111.111-11" not in text and "90000-0001" not in text and "@" not in text
    assert text.count(MASK) == 3


def test_policyholder_names_are_masked_but_other_names_are_not():
    s = PIIScrubber(["Marta Ferreira Bittencourt", "Rogério Alcântara Nunes"])
    text, hits = s.scrub("O sinistro de Marta Ferreira Bittencourt foi pago; Rogério Nunes está em regulação.")
    assert hits == 2 and "Marta" not in text and "Rogério" not in text
    text2, hits2 = s.scrub("Conforme a Política de Privacidade e as Condições Gerais do Seguro Auto.")
    assert hits2 == 0 and text2.startswith("Conforme a Política de Privacidade")


def test_partial_name_prefix_is_masked():
    s = PIIScrubber(["Marta Ferreira Bittencourt"])
    text, hits = s.scrub("Segurada: Marta Ferreira")
    assert hits == 1 and "Marta" not in text


def test_claim_numbers_and_amounts_survive():
    s = PIIScrubber(["Marta Ferreira Bittencourt"])
    text, hits = s.scrub("SIN-2025-004512 pagou R$ 100.000,00 em 2025-04-05 (apólice AP-AUTO-000001).")
    assert hits == 0 and "SIN-2025-004512" in text and "100.000,00" in text
