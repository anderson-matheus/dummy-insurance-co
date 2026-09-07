import json

import pytest

from app.chat.orchestrator import Completed, Delta, Deps, Reset, SourcesUpdate, Stage, answer_question, new_run
from app.core.config import Settings
from app.core.errors import LLMUnavailable, ResponseTruncated
from app.knowledge.claims_db import ClaimsDB
from app.knowledge.pii import PIIScrubber, load_policyholder_names
from app.knowledge.retriever import Retriever
from app.llm.fake_provider import Fail, FakeProvider, answer, malformed_tool_call, provider_refusal, refuse, tool_call, truncated
from app.llm.resilient import ResilientLLM
from tests.conftest import CLAIMS_DB

VIGENCIA = "Qual é o prazo de vigência padrão de uma apólice de Seguro Auto?"


@pytest.fixture(scope="module")
def knowledge(index_path):
    retriever = Retriever(index_path)
    claims = ClaimsDB(str(CLAIMS_DB))
    scrubber = PIIScrubber(load_policyholder_names(str(CLAIMS_DB)))
    yield retriever, claims, scrubber
    retriever.close()


def make_deps(knowledge, provider, **overrides):
    retriever, claims, scrubber = knowledge
    kwargs = dict(llm_api_key="k", question_deadline_s=30, llm_max_retries=0, max_tool_iterations=3, max_tool_repairs=1)
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    return Deps(settings=settings, llm=ResilientLLM(provider, settings), retriever=retriever, claims_db=claims, scrubber=scrubber)


async def run(deps, question, history=()):
    r = new_run(deps)
    events = [ev async for ev in answer_question(question, list(history), deps, r)]
    return events, r


def streamed_text(events):
    text = ""
    for ev in events:
        if isinstance(ev, Reset):
            text = ""
        elif isinstance(ev, Delta):
            text += ev.text
    return text


async def test_happy_path_streams_and_cites(knowledge):
    provider = FakeProvider([answer("A vigência padrão do Seguro Auto é de 12 meses [1].")])
    deps = make_deps(knowledge, provider)
    events, r = await run(deps, VIGENCIA)
    assert [e.name for e in events if isinstance(e, Stage)] == ["retrieving", "generating"]
    sources = next(e for e in events if isinstance(e, SourcesUpdate)).sources
    assert sources[0]["doc_code"] == "CG-AUTO-2024" and sources[0]["n"] == 1
    done = events[-1]
    assert isinstance(done, Completed) and done.status == "complete"
    assert done.content == "A vigência padrão do Seguro Auto é de 12 meses [1]."
    assert streamed_text(events) == done.content  # progressive deltas reproduce the final text
    assert [c.ordinal for c in done.citations] == [1] and done.citations[0].doc_code == "CG-AUTO-2024"
    assert done.citations[0].doc_status == "vigente" and done.citations[0].page == 2
    assert done.metrics.llm_calls == 1 and done.metrics.input_tokens == 120
    assert done.provenance["documents_cited"][0]["doc_code"] == "CG-AUTO-2024" and done.provenance["database_queried"] is False
    # the model saw numbered sources and the compact system prompt
    req = provider.calls[0]
    assert "[1] CG-AUTO-2024 v3.2" in req.messages[-1]["content"] and "VIGENTE" in req.messages[-1]["content"]
    assert "payments.paid_amount" in req.system and len(req.tools) == 3


async def test_invalid_markers_are_dropped(knowledge):
    provider = FakeProvider([answer("Doze meses [1] conforme a cláusula [99].")])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    done = events[-1]
    assert done.content == "Doze meses [1] conforme a cláusula." and [c.ordinal for c in done.citations] == [1]


async def test_missing_citation_triggers_one_repair_then_succeeds(knowledge):
    provider = FakeProvider([answer("A vigência é de 12 meses."), answer("A vigência é de 12 meses [1].")])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    assert len(provider.calls) == 2
    repair_req = provider.calls[1]
    assert repair_req.tool_choice == "none" and "não citou" in repair_req.messages[-1]["content"]
    assert any(isinstance(e, Reset) for e in events)
    assert events[-1].status == "complete" and events[-1].citations


async def test_persistently_uncited_answer_becomes_refusal(knowledge):
    provider = FakeProvider([answer("A vigência é de 12 meses."), answer("Continua sem fonte.")])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    done = events[-1]
    assert done.status == "refused" and done.refusal_code == "NO_VERIFIABLE_SOURCE"
    assert "Continua sem fonte" not in done.content and done.citations == []
    assert streamed_text(events) == done.content


async def test_refuse_tool_is_terminal_and_pii_scrubbed(knowledge):
    msg = "Não posso listar dados pessoais como os de Marta Ferreira Bittencourt (POL-LGPD-2024)."
    provider = FakeProvider([refuse("PII_REQUEST", msg), answer("never")])
    events, r = await run(make_deps(knowledge, provider), "Liste o nome e o CPF dos segurados da ata de abril de 2025.")
    done = events[-1]
    assert done.status == "refused" and done.refusal_code == "PII_REQUEST"
    assert "Marta" not in done.content and "[dado pessoal omitido]" in done.content
    assert len(provider.calls) == 1 and r.pii_hits == 1


async def test_db_tool_loop_uses_paid_amount(knowledge):
    sql = "SELECT c.claim_number, c.claim_amount, p.paid_amount FROM claims c JOIN payments p ON p.claim_id = c.claim_id WHERE c.claim_number = 'SIN-2025-004512'"

    def router(req):
        last = req.messages[-1]
        if last["role"] == "tool":
            n = last["content"].split("fonte [")[1].split("]")[0]
            return answer(f"O valor pago foi R$ 100.000,00 [{n}], exatamente o limite da RCF-DM [2].")
        return tool_call("query_claims_db", {"sql": sql, "purpose": "valor pago"})

    provider = FakeProvider(router=router)
    events, r = await run(make_deps(knowledge, provider), "No sinistro SIN-2025-004512, o valor pago respeitou o limite da RCF-DM?")
    done = events[-1]
    assert done.status == "complete" and len(provider.calls) == 2
    db_cit = next(c for c in done.citations if c.source_type == "database")
    assert db_cit.sql == sql and db_cit.row_count == 1 and "100000.00" in db_cit.snippet
    assert done.provenance["database_queried"] and done.provenance["database_snapshot"].startswith("dados até 2026-02-11")
    assert "querying_db" in [e.name for e in events if isinstance(e, Stage)]
    assert r.tool_calls == 1
    # tool result was fed back with the right shape
    assert provider.calls[1].messages[-1]["role"] == "tool" and provider.calls[1].messages[-2]["role"] == "assistant"


async def test_sql_guard_rejection_is_fed_back_as_tool_error(knowledge):
    provider = FakeProvider([
        tool_call("query_claims_db", {"sql": "SELECT name FROM policyholders LIMIT 1", "purpose": "x"}),
        refuse("PII_REQUEST", "Não é possível consultar dados pessoais."),
    ])
    events, _ = await run(make_deps(knowledge, provider), "Qual o nome do segurado do sinistro SIN-2025-004512?")
    err = json.loads(provider.calls[1].messages[-1]["content"])
    assert "dados pessoais" in err["error"]
    assert events[-1].status == "refused"


async def test_malformed_tool_arguments_are_repaired(knowledge):
    provider = FakeProvider([malformed_tool_call("search_documents"), answer("Doze meses [1].")])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    err = json.loads(provider.calls[1].messages[-1]["content"])
    assert "argumentos inválidos" in err["error"] and events[-1].status == "complete"


async def test_search_tool_adds_numbered_sources(knowledge):
    def router(req):
        if req.messages[-1]["role"] == "tool":
            n = req.messages[-1]["content"].split("[")[1].split("]")[0]
            return answer(f"A franquia de vidros no residencial é R$ 100,00 [{n}].")
        return tool_call("search_documents", {"query": "franquia vidros residencial", "product": "Residencial"})

    provider = FakeProvider(router=router)
    events, _ = await run(make_deps(knowledge, provider), "Qual é o prazo de vigência de uma apólice de Seguro Auto?")
    updates = [e for e in events if isinstance(e, SourcesUpdate)]
    assert len(updates) == 2 and len(updates[1].sources) > len(updates[0].sources)
    assert events[-1].status == "complete" and events[-1].citations[0].doc_code in {"CG-RES-2024", "NI-022"}


async def test_text_before_tool_call_is_reset(knowledge):
    provider = FakeProvider([tool_call("search_documents", {"query": "vigência"}, text="Vou verificar..."), answer("Doze meses [1].")])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    assert any(isinstance(e, Reset) for e in events)
    assert streamed_text(events) == "Doze meses [1]."


async def test_iteration_cap_forces_final_answer(knowledge):
    provider = FakeProvider(router=lambda req: answer("Doze meses [1].") if req.tool_choice == "none" else tool_call("search_documents", {"query": "vigência"}))
    events, _ = await run(make_deps(knowledge, provider, max_tool_iterations=2), VIGENCIA)
    assert len(provider.calls) == 3 and provider.calls[-1].tool_choice == "none" and events[-1].status == "complete"


async def test_pii_in_answer_is_scrubbed_even_when_streamed(knowledge):
    provider = FakeProvider([answer("O sinistro de Marta Ferreira Bittencourt (CPF 111.111.111-11) foi pago no limite [1].", chunk=5)])
    events, r = await run(make_deps(knowledge, provider), "No sinistro SIN-2025-004512, o valor pago respeitou o limite?")
    done = events[-1]
    assert "Marta" not in done.content and "111.111" not in done.content
    assert "Marta" not in streamed_text(events) and streamed_text(events) == done.content
    assert r.pii_hits >= 2


async def test_truncated_answer_raises_with_partial(knowledge):
    provider = FakeProvider([truncated("A vigência padrão é de doze meses, contados a partir das 24 horas da data de início")])
    deps = make_deps(knowledge, provider)
    r = new_run(deps)
    with pytest.raises(ResponseTruncated):
        async for _ in answer_question(VIGENCIA, [], deps, r):
            pass
    assert r.partial_text.startswith("A vigência padrão")


async def test_mid_stream_provider_failure_keeps_partial(knowledge):
    provider = FakeProvider([Fail(LLMUnavailable(), after_text="A vigência padrão é de doze meses e a renovação não é automática")])
    deps = make_deps(knowledge, provider)
    r = new_run(deps)
    with pytest.raises(LLMUnavailable) as exc:
        async for _ in answer_question(VIGENCIA, [], deps, r):
            pass
    assert exc.value.partial and r.partial_text.startswith("A vigência padrão") and r.metrics().llm_calls == 0


async def test_provider_content_filter_becomes_refusal(knowledge):
    provider = FakeProvider([provider_refusal()])
    events, _ = await run(make_deps(knowledge, provider), VIGENCIA)
    assert events[-1].status == "refused" and events[-1].refusal_code == "PROVIDER_REFUSAL"


async def test_history_is_passed_as_plain_turns(knowledge):
    from app.history.repo import Message

    def msg(role, content):
        return Message(id=role, conversation_id="c", seq=1, role=role, content=content, status="complete", client_message_id=None,
                       parent_message_id=None, attempt=1, error_code=None, refusal_code=None, provenance=None, created_at="", completed_at=None)

    provider = FakeProvider([answer("Também 12 meses [1].")])
    history = [msg("user", "Qual a vigência do auto?"), msg("assistant", "12 meses [1]." * 200)]
    await run(make_deps(knowledge, provider), "E no residencial?", history)
    sent = provider.calls[0].messages
    assert sent[0] == {"role": "user", "content": "Qual a vigência do auto?"}
    assert sent[1]["role"] == "assistant" and len(sent[1]["content"]) == 600
    assert sent[2]["role"] == "user" and "E no residencial?" in sent[2]["content"]
