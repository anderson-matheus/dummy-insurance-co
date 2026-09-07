# Evaluation

## Method

- **Cases**: the 10 given golden cases (`evals/golden_set.json`, unchanged) plus 14 authorial cases
  (`evals/authorial_cases.json`) that probe what the golden set does not: the superseded-version trap
  (current notice period and an explicit question about v1.0), RCF-DC vs RCF-DM, a residential deductible, the
  readjustment band for 45% loss ratio, underwriting authority, rental-car days, two database questions
  (count of claims under adjustment; claimed vs paid amount of a claim), PII through the database
  ("telefone do segurado do sinistro…"), an out-of-corpus question (IOF), a prompt-injection attempt, small
  talk, and an incendiary-claim adjustment deadline.
- **Checks are deterministic** (`evals/golden_checks.json`, encoding each case's *critério de acerto*):
  required phrases (any of a group), required cited document (and version, for NI-014), required database
  citation and `paid_amount` in the SQL, forbidden values unless explained (e.g. `150.000` only when RCF-DC is
  mentioned), mandatory refusal, and regexes that must never match (CPF, phone, policyholder names). No
  LLM-as-judge: it would double the calls under a 50-requests/day quota and the criteria are
  keyword/citation/refusal shaped. A case passes only if all its checks pass and the answer status is
  `complete` or `refused`.
- **Runner**: `python -m scripts.run_evals` (from `backend/`) runs the orchestrator in-process with the
  configured provider, pauses 3.5 s between cases (20 req/min), writes one JSON per case to
  `evals/results/latest/` and resumes from them, and renders `evals/results/report.md`.
  `--dry-run` uses the scripted fake provider to validate the harness without network.
- **Cost** is reported as tokens and as an *equivalent* list-price cost using `LLM_INPUT_PRICE_PER_M` /
  `LLM_OUTPUT_PRICE_PER_M` (0 on the free tier); latency is wall-clock per question including retrieval,
  tool execution and all LLM calls.

## Retrieval evidence (no LLM involved)

`backend/tests/test_retriever.py` asserts, for every golden question and the NI-014 variants, that the chunk
containing the expected answer is among the results handed to the model. Ranks measured on the 24-question
tuning set (top-8 plus referenced tables):

| question | supporting chunk | rank |
|---|---|---|
| gs-001 vigência Auto | CG-AUTO-2024 §2.1 | 1 |
| gs-002 fraude / SIU | MAN-SIN-2025 §6.2 | 1 |
| gs-003 furto qualificado | CG-RES-2024 §3.3 | 1 |
| gs-004 RCF-DM | CG-AUTO-2024 Tabela 1 | 3 |
| gs-005 roubo 45 dias | MAN-SIN-2025 Tabela 1 | 3 |
| gs-006 raio / franquia | NI-022 Tabela 1 + nota (3) | 3 |
| gs-007 SIN-2025-004512 | CG-AUTO-2024 Tabela 1 (+ DB tool) | 2 |
| gs-008 desconto à vista | none (correctly nothing relevant) | — |
| gs-009 vidros | CG-AUTO-2024 Tabela 1 / CG-RES-2024 Tabela 1 | 6 / 4 |
| gs-010 PII | POL-LGPD-2024 §3 | 1 |
| au-01 prazo de aviso (vigente) | NI-014 v2.0 §3 | 3–4 |
| au-02 prazo na versão 1.0 | NI-014 v1.0 §3 | 1 |

## Provider run

_Pending: the run against the configured free provider needs `LLM_API_KEY` in `.env`. Once available,
`make evals` fills `evals/results/report.md`; the table, aggregates and analysis are copied here._

The harness itself was validated with `make evals ARGS="--dry-run"` (scripted provider): all 24 cases execute,
refusal cases (gs-008, gs-010, au-10…au-13) pass, and content cases fail as expected because the fake
answers carry no facts — which confirms the checks are not trivially satisfiable.
