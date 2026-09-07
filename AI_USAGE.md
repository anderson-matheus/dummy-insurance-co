# AI usage in this project

## Summary

This PoC was built in a single Claude Code session (model: Claude Fable 5.1) **directed and reviewed by the
candidate**. The AI wrote the plan, the code, the tests, the documentation and the diagram source; the
candidate set the requirements, took the product and infrastructure decisions, questioned intermediate
results, and approved the plan before implementation. Nothing in the repository was written outside that
loop, and this document is intentionally explicit about it.

## Where and how AI was used

| phase | how |
|---|---|
| Understanding the case | The assistant extracted the kit, read all 13 PDFs (text), the data dictionary, the golden set and profiled `claims.db` (distributions, the `claim_amount` vs `paid_amount` trap, date ranges) before proposing anything. |
| Planning | Three parallel planning sub-agents (backend robustness/API/history; ingestion/retrieval/prompt/PII/evals; frontend/compose/docs) produced designs; the main session merged them into a written plan that the candidate approved. The retrieval planner prototyped FTS5 on the corpus before recommending it. |
| Decisions taken by the candidate | Free LLM provider instead of a paid key; OpenRouter over Groq/Gemini; UI in pt-BR with documentation in English; incremental commits on `main`; deleting the PyCharm placeholder and the zip after extraction. |
| Implementation | Backend, frontend, Docker, scripts and tests were generated incrementally, each step run against real data (the corpus and the database) and committed only after its tests passed. |
| Verification | 90 backend tests and 7 frontend tests (no network); the full Compose stack exercised through nginx with the offline stub provider (streaming, DB tool, every failure drill, history reload); Docker builds. |
| Documentation | README, DECISIONS, EVALS, this file and the Graphviz diagram. |

## Suggestions that were rejected or corrected

1. **Anthropic Claude as the provider (rejected by the candidate).** The initial plan and the SDK guidance
   defaulted to `claude-opus-5` / Claude Sonnet with prompt caching. The candidate chose a free provider, so
   the provider layer was redesigned around the OpenAI-compatible protocol, the prompt was made compact
   (no caching to lean on), and rate limiting (429) became the primary failure mode to design for.
2. **Groq as the default free provider (corrected).** After the redirection the planner proposed Groq; the
   candidate chose OpenRouter. The runtime consequence — 50 requests/day — forced the evaluation runner to
   persist and resume per case and to keep development on the fake provider.
3. **Retrieval boost weights from the planner (corrected with measurements).** The planner suggested
   product ×1.25/×0.85 and doc-code ×1.6 boosts. A rank check on 24 questions showed the NI-014 notice-period
   section missing from the top results (BM25 favoured shorter sibling sections) and the explicit-version
   question failing. A coverage/section-title rescoring was added and the weights were re-tuned by a grid
   search (product 1.1/0.9, code 2.5, version 2.0, candidate pool widened from 40 to 200); the "aviso ↔
   comunicação" synonym pair fixed the last miss.
4. **Stem columns in the FTS index (simplified).** The planner proposed indexing Snowball-stemmed copies of
   every text column. Prefix queries on stemmed query terms (`"vigenc"*`) give the same recall on this corpus
   with a simpler index, so the stem columns were dropped.
5. **Extractor bug in generated code (caught by inspection).** The first PDF extractor re-sorted blocks by
   vertical position *after* computing the two-column reading order, silently interleaving the FAQ columns; a
   dump of the chunks showed a question cut mid-sentence. Fixed by ordering tables together with text blocks.
   A second defect in the same pass (table captions not detected on multi-line chunks) was caught the same way.
6. **Test double recording a live reference (caught by a failing test).** The fake provider stored the
   orchestrator's message list by reference, so a later assertion saw messages appended after the call. The
   fake now snapshots each request.
7. **Evaluation dry-run router matched "SIN-" inside the sources block instead of the question** (caught by
   running the harness); scoped to the question line.
8. **Persisting partial answers on failure (design gap found while writing API tests).** The first
   orchestrator only kept the *emitted* text, so a failure during the PII hold-back window lost the last 40
   characters; the run now persists the scrubbed full buffer.
9. **Docker build failures blamed on requirements (misdiagnosis corrected).** The compose build failed on
   `pip install`; the assistant first suspected a pin, then read the full log: the build network had no DNS
   on this host. Builds now use `network: host`, documented as an environment workaround, not a code fix.
10. **Frontend planner's "treat persisted `streaming` rows as interrupted" (adjusted).** A reply can still be
    running on the server (another tab); the UI now shows "em andamento" and polls until it settles, and only
    rows swept at restart are marked interrupted.

## Limits of this approach

- The candidate did not hand-write the code; the review happened at the level of design decisions, test
  results, produced artefacts (chunk dumps, retrieval ranks, API responses through nginx) and the reading of
  the generated modules. Bugs 5–9 above were found by that loop, which is the argument for keeping it.
- The evaluation checks are deterministic and were also AI-generated from the golden set's criteria; they
  are conservative (keyword/citation/refusal), so a correct answer phrased unusually can fail a check and is
  then reviewed by hand (see EVALS.md).
