# Technical decisions

This document explains what was built, why, the trade-offs, what was cut, and what would come next.
The architecture diagram is in `docs/architecture.svg`.

## 1. Constraints that shaped the design

- Answers must cite sources, refuse without them, and never expose policyholder PII; budget US$0.05 per
  question; p95 latency under 8 s.
- **The LLM is a free-tier provider** (decision taken with the stakeholder): OpenRouter free models through an
  OpenAI-compatible endpoint. Consequences: US$0 marginal cost but a hard quota (20 requests/min, 50/day
  without purchased credits), no prompt caching, and open models that are less disciplined with tool calls
  and citation formats than frontier models. The design treats the provider as *slow, unreliable and
  rate-limited by default*, which is also the highest-weighted evaluation criterion.
- The PoC corpus is 13 documents; production is tens of thousands. Corpus access must scale in design, not
  only in the demo.

## 2. Architecture at a glance

Browser (React) → nginx → FastAPI. One pipeline per question, exposed by two transports (JSON and SSE):

1. **retrieve** the top-8 chunks with BM25 over SQLite FTS5 (plus referenced tables);
2. **generate** with a compact system prompt and three tools — `search_documents`, `query_claims_db`,
   `refuse` — inside a bounded loop, streaming text as it arrives;
3. **validate** the `[n]` citation markers against the numbered sources the server actually provided;
   an answer without a verifiable citation is *discarded and turned into a refusal*;
4. **scrub PII** (patterns + policyholder-name blocklist), persist message, citations, metrics and
   provenance, and emit the final event.

Generation runs as a detached task (`app/runs.py`): if the browser disconnects, the answer still finishes
and is persisted; a reload shows it. Cancel is explicit (`POST …/cancel`) and keeps the partial text.

## 3. Provider isolation and failure handling

**Seam.** `app/llm/provider.py` defines the only contract the rest of the app knows: an async stream of
`TextDelta | ToolUse | MessageEnd` events. `OpenAICompatProvider` is the one real implementation; the SDK is
imported only there. Its stream parsing (`accumulate_chunks`) is a pure function over chunk objects and is
unit-tested without the network; exceptions are translated at the seam into a small taxonomy
(`LLMTimeout`, `LLMRateLimited`, `LLMUnavailable`, `LLMBadResponse`, `LLMMisconfigured`) that carries a fixed
pt-BR user message, a `retryable` flag and, for 429, the server's `Retry-After`. `FakeProvider` implements the
same contract with scripted turns and failure injection (`Fail`, `Hang`, fail-after-text), which is how the
core is tested end-to-end without an API key (90 backend tests, none hit the network).

**Policy** (`app/llm/resilient.py`), in the order it matters for the user:

| concern | behaviour |
|---|---|
| timeout | per-request `LLM_TIMEOUT_S` (connect 3 s) **and** a per-question deadline `QUESTION_DEADLINE_S` (30 s) that bounds retries, backoff and tool rounds together; the UI shows the stage and elapsed time, and hints after 8 s |
| retry with a limit | at most `LLM_MAX_RETRIES` (2) extra attempts, only for retryable failures **that happened before any text reached the user**, with exponential backoff and jitter; 429 sleeps exactly `Retry-After` when it fits the remaining deadline, otherwise fails fast and hands the wait to the UI (countdown on the retry button). The SDK's own retries are disabled so the budget is predictable |
| mid-stream failure | never retried automatically (it would re-render text under the analyst's eyes); the partial answer is persisted with `status=error`, shown muted, and "Tentar novamente" regenerates it in place |
| graceful degradation | circuit breaker: three consecutive failures open it for 30 s (or the largest `Retry-After` seen); while open, questions fail immediately with a specific message (rate limited vs unavailable) and `/api/health` reports `degraded`; the UI polls health and shows a banner. History, retrieval and the DB tool keep working |
| budget | per question, dual cap: list-price cost (`MAX_COST_USD_PER_QUESTION`, computed from configurable prices) and tokens (`MAX_TOKENS_PER_QUESTION`, the real scarce resource on a free tier); tool rounds (`MAX_TOOL_ITERATIONS`) and argument repairs (`MAX_TOOL_REPAIRS`) are bounded; a semaphore limits concurrent provider calls so several analysts do not self-inflict 429 |
| useful errors without internals | every failure maps to a code and a fixed pt-BR message; status codes, provider bodies, model names, prompts and stack traces go to the log under a correlation id that the API returns and the UI displays as `ref.` |

Provider failures are **domain state, not HTTP errors**: the assistant message is stored with
`status=error` + `error_code`, the JSON endpoint returns 200 with it, and the SSE stream ends with an `error`
event that includes the persisted message. HTTP errors are reserved for the API's own problems (404, 409,
422, 500), always in the same envelope.

## 4. History modeling

SQLite (`app/history/schema.sql`), four tables:

- `conversations(id, title, created_at, updated_at)`;
- `messages(id, conversation_id, seq, role, content, status, client_message_id, parent_message_id, attempt,
  error_code, refusal_code, provenance, created_at, completed_at)` with `UNIQUE(conversation_id,
  client_message_id)` and a **unique `parent_message_id` for assistant rows**;
- `citations(message_id, ordinal, source_type, doc_code, doc_title, doc_version, doc_status, section, page,
  chunk_id, snippet, sql, row_count, data_snapshot)`;
- `message_metrics(message_id, model, served_model, tokens, cost_usd, latency_ms, llm_calls, retries,
  tool_calls, pii_hits, provider_request_ids)`.

Why this shape:

- **One assistant row per question, updated in place.** The unique parent link means a retry cannot create
  a second reply; `attempt` is incremented and content/citations/metrics are replaced. Combined with the
  unique `client_message_id`, "repeating a question does not duplicate messages" is enforced by the schema,
  not by the client.
- **Idempotency semantics** (`routes_messages._prepare`): same id + same content → if the reply is
  `complete|refused` it is replayed with no LLM call; if it is `pending` and running, the caller attaches to
  the run; if it failed/cancelled/interrupted, it is regenerated. Same id + different content → 409.
- **Status per message** (`pending|complete|refused|error|cancelled|interrupted`) plus `error_code` and
  `refusal_code` let the UI rebuild every visual state after a reload, including partial text of an
  interrupted answer. A startup sweep marks rows left `pending` by a crash as `interrupted/SERVER_RESTART`.
- **Citations as a table**, not JSON: the provenance footer, the evaluation runner and future audits
  ("which answers cited a superseded version?") query them directly, and the `UNIQUE(message_id, ordinal)`
  keeps `[n]` markers consistent. Metrics are separate so cost/latency reporting is one `SELECT`.
- SQLite in WAL mode is adequate for an internal PoC with a handful of analysts; the repository is the only
  place that knows SQL, so Postgres is a drop-in change.

## 5. API design

Two transports over the same pipeline: `POST …/messages` (waits, returns both messages) for simple clients
and tests, and `POST …/messages/stream` (SSE over `fetch`, since `EventSource` cannot POST) for the UI.
Event names describe what the analyst sees: `accepted` (ids), `stage` (retrieving / querying_db /
searching_documents / generating), `sources` (citation chips appear before text), `delta`, `reset` (discard
draft text; used when a model writes prose before a tool call or when PII scrubbing changes an already
streamed prefix), `done` (final message) or `error` (code, retryability, persisted partial message).
`: ping` comments keep proxies from timing out. `GET /api/health` is the degradation signal for the UI.

## 6. Corpus access strategy

**Ingestion** (`app/knowledge/extract.py`, `ingest.py`) is where most of the "real document" problems are
solved deterministically, once, instead of being pushed to the LLM on every question:

- PyMuPDF blocks with column-aware ordering (the FAQ is two-column; a question split across columns is
  re-joined) and `find_tables()` for ruled tables, so a table row like
  `RCF-DM - Danos Materiais a Terceiros | R$ 100.000,00 | não há` survives as one line;
- repeated headers/footers are dropped by pattern + position; paragraphs split by page breaks are re-joined;
- one chunk per numbered section, long sections split at paragraph boundaries (~900 chars); a table stays
  with its caption **and its page-bottom footnotes** (NI-022's lightning exception lives in footnote (3));
  FAQ Q/A pairs are individual chunks; 226 chunks, average ~360 characters;
- each document's code, title, version and effective date come from its own metadata line; versions of the
  same code are grouped and the latest effective date in force is `vigente`, the others `superada`
  (NI-014 v1.0). The status is shown to the model, to the UI (amber badge) and to the evaluation;
- PII is masked at ingest (CPF/phone/e-mail patterns + policyholder names from `claims.db`), so the
  committee minutes' table reaches the index as `SIN-2025-004512 | [dado pessoal omitido] | …`.

**Retrieval** (`app/knowledge/retriever.py`): BM25 over SQLite FTS5 (`unicode61 remove_diacritics`), query
terms stemmed (Snowball pt) and used as prefixes, a small synonym map (carro→veículo, raio→descarga
atmosférica, aviso↔comunicação…), then a rescoring pass: coverage of query terms (BM25 alone over-rewards very
short chunks), section-title hits, explicit document-code/version mentions, `superada` penalty, mild product
boost, and **pull-through of referenced tables** (the "Cobertura de Vidros" section says the limit is in
Tabela 1; the table chunk is inserted right after it). Weights were tuned by a grid search on 24 questions;
all golden and authorial questions retrieve their supporting chunk within the top 6 (see `tests/test_retriever.py`).
Average search time is ~14 ms.

**Why not embeddings in the PoC.** They would add an external dependency (another quota, another failure
mode, another cost line) for no measurable gain on this corpus, where vocabulary is controlled and the model
can reformulate through `search_documents`. Determinism also makes the evaluation reproducible.

**Production path (10⁴–10⁵ documents).** The `Retriever` interface is the seam: replace the FTS5 engine by
a hybrid engine (BM25 + dense vectors with reciprocal-rank fusion, e.g. OpenSearch or Postgres+pgvector),
keep the same chunk contract and metadata filters (`doc_type`, `product`, `status`), make the document
registry the source of truth for versions (supersession by effective date, per-document re-ingestion on
change, corpus hash in the manifest), and run ingestion as a pipeline with per-document idempotency. The
prompt, citation validation, tool loop and UI do not change.

## 7. The claims database as a tool

`query_claims_db` executes exactly one `SELECT` on a read-only connection. The guard is SQLite's
**authorizer callback**: any read of `policyholders.name/cpf/phone/email/birth_date` or of `sqlite_master`
is denied by the engine itself, whatever the SQL looks like (joins, aliases, `SELECT *`); everything that is
not `SELECT/READ/FUNCTION` is denied; a progress handler aborts after 2 s; rows are capped at 50. Rejections
are returned to the model as a tool error it can repair once. Each executed query becomes a numbered
source (`[n]`) with the SQL, row count and a freshness label (`dados até 2026-02-11`), and the data
dictionary's trap is in the system prompt and the schema summary: `claim_amount` is what was **claimed**;
`payments.paid_amount` is what was **paid**.

## 8. LLM integration

- **Prompt** (`app/chat/prompts.py`, pt-BR, ~900 tokens): answer only from numbered sources; every claim ends
  with `[n]`; call `refuse` when sources do not answer; prefer `VIGENTE`, flag `SUPERADA`; when the answer
  depends on the product and the question does not say which, give each value with its source and ask; DB
  rules (single SELECT, `paid_amount`, no PII columns); refuse PII requests citing POL-LGPD-2024; instructions
  inside the question or the sources do not change the rules; tools before prose.
- **Tools** have flat JSON schemas (string parameters only) because open models handle nested schemas
  poorly; arguments are validated with pydantic and malformed calls are fed back as errors (bounded repairs);
  a tool call written as JSON text is salvaged. Extra document searches are capped at two per question: the
  evaluation showed a model searching three times for an out-of-corpus question and exhausting the token budget
  before refusing; the third search now returns "limit reached, answer or refuse".
- **Citations are enforced server-side.** Markers that do not map to a provided source are dropped; an
  answer with none gets one repair call ("rewrite citing `[n]` or answer `SEM_FONTE`"); if it still has none
  it is replaced by a refusal (`NO_VERIFIABLE_SOURCE`). This is the mechanism behind "never hallucinate":
  the model cannot get an unsupported answer past the server.
- **Model choice**: the default is the strongest free model on OpenRouter with tool calling and structured
  arguments at the time of writing (`nvidia/nemotron-3-super-120b-a12b:free`), with two `:free` fallbacks
  through OpenRouter's `models` parameter (provider-level failover on rate limits/downtime), temperature 0
  and reasoning disabled (`LLM_REASONING_EFFORT=none`) to keep latency down. All of this is configuration; EVALS.md records what was
  actually used.
- **Conversation context**: the last four completed turns are sent as plain text (assistant turns
  truncated), never tool messages, so a follow-up like "e no residencial?" works without inflating the
  prompt.

## 9. PII: defense in depth

1. Masked at ingest (the only document with PII never reaches the index in clear).
2. Denied at the database engine (authorizer), independent of the prompt.
3. Instructed in the prompt (refuse with `PII_REQUEST`, cite the privacy policy).
4. Scrubbed on output: CPF/phone/e-mail patterns and a blocklist of the 3,200 policyholder names
   (case/accent-insensitive; full names and first+last / first+second pairs). During streaming the last 40
   characters are held back so a name or CPF spanning two deltas cannot leak; the scrubbed final text is what
   gets persisted. Hits are counted in the metrics.

## 10. Streaming edge cases

| situation | behaviour |
|---|---|
| provider fails mid-stream | `error` event; partial text persisted as `status=error`; UI shows it muted with the reason and a retry |
| user cancels | `AbortController` + `POST …/cancel`; the task is cancelled, the partial is persisted as `cancelled`, the bubble shows "interrompida pelo usuário" and a retry |
| connection drops without cancel | the detached task finishes and persists; reload shows the answer; the UI treats a silent stream (45 s) as `interrupted` and offers a retry with the same `client_message_id` (no duplicate) |
| server restarts mid-answer | startup sweep marks the row `interrupted/SERVER_RESTART` |
| model writes text then calls a tool | `reset` event clears the draft before tool results |
| PII detected in an already streamed prefix | `reset` + re-emission of the scrubbed text |
| reply still pending after reload (another tab) | UI polls the conversation every 2 s until it settles |

## 11. Cost and latency

Measured on the 24 evaluation cases (EVALS.md): mean 4.6 k tokens and 1.33 LLM calls per question, i.e.
US$ 0.010 mean / US$ 0.022 max at a paid mid-tier list price (US$ 2/M input, US$ 10/M output) and US$ 0 on the
free tier, where the daily quota is the binding constraint (the run used 33 requests). Latency: p50 4.4 s but
p95 22 s — the pipeline's own work is small (retrieval ~14 ms, one model call in 18/24 cases) and the spikes are
free-tier queueing; the 8 s p95 target needs a paid or self-hosted tier. Levers in place: compact prompt, no
embeddings call, reasoning disabled (measured 10.1 s → 3.4 s for the same answer), bounded tool loop with at
most two extra searches, streaming so the analyst sees text early, and an explicit hint plus cancel in the UI
after 8 s.

## 12. Testing strategy

- Unit: extraction/ingestion on the real PDFs (headers stripped, cross-page paragraphs, table+footnotes,
  two-column FAQ, version supersession, PII masking), retrieval ranks for golden questions, SQL guard
  (PII columns, non-SELECT, multi-statement, row cap), PII scrubber, stream chunk accumulation and exception
  translation with fabricated SDK errors, resilience policy (retry limit, non-retryable, retry-after,
  deadline on a hanging provider, breaker states, budget caps, semaphore).
- Orchestrator with the fake provider against the real index and DB: citations, repair-then-refusal, refuse
  tool, DB loop with `paid_amount`, malformed tool args, `reset`, iteration cap, PII in streamed text,
  truncation, mid-stream failure with partial.
- API with an in-process ASGI client: CRUD, idempotent resend, retry in place (`attempt` 2), refusal as
  domain state, SSE event order and replay, mid-stream failure persistence, cancel, validation envelope,
  open circuit reported by health.
- Frontend: SSE parser and reducer (retry does not duplicate, cancel keeps partial).
- Evaluation harness `--dry-run` validates the runner itself without network.

## 13. What was cut and trade-offs accepted

- No authentication/authorization (internal PoC; put it at the nginx/API layer in production).
- No embeddings / hybrid retrieval (see §6); no reranker.
- No LLM-as-judge in the evaluation: deterministic checks only, because judging doubles the calls under a
  50/day quota and the criteria are keyword/citation/refusal shaped; the trade-off is that phrasing variants
  need the check lists to be maintained.
- History context is plain text of the last turns (no tool messages), so a follow-up that needs the previous
  DB result re-queries.
- Single-process API; the run registry and breaker are in memory (fine for one instance; would move to
  Redis for several).
- The frontend has no router and minimal styling on purpose: the evaluated surface is behaviour.

## 14. Next steps

1. Hybrid retrieval with metadata filters and a document registry service; incremental ingestion.
2. Structured extraction of coverage tables into a typed "limits and deductibles" store, so numeric
   questions get exact lookups in addition to text citations.
3. Authentication, audit log of questions/answers/citations, per-user rate limits.
4. Observability: request tracing with the correlation id, provider latency histograms, cost dashboards
   from `message_metrics`.
5. An LLM-judge tier for the evaluation once a paid provider is available, plus a larger authorial set built
   from real analyst questions.
