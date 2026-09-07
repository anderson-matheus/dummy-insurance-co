"""Evaluation runner: golden set + authorial cases -> per-case JSON -> markdown report.

Usage (from backend/):
  python -m scripts.run_evals                # real provider from .env, resumes from evals/results
  python -m scripts.run_evals --dry-run      # scripted fake provider (harness check, no network)
  python -m scripts.run_evals --only gs-007 --fresh --pause 3.5

The free-tier quota (20 req/min, 50 req/day) shapes this: one JSON per case is
written immediately, completed cases are skipped on re-runs, and a pause is
kept between cases.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.chat.orchestrator import Completed, Deps, answer_question, new_run
from app.core.config import REPO_ROOT, Settings
from app.core.errors import AppError
from app.knowledge.claims_db import ClaimsDB
from app.knowledge.ingest import build_index
from app.knowledge.pii import PIIScrubber, load_policyholder_names
from app.knowledge.retriever import Retriever
from app.llm.resilient import ResilientLLM

EVALS_DIR = REPO_ROOT / "evals"
RESULTS_DIR = EVALS_DIR / "results"


def load_cases(which: set[str]) -> list[dict]:
    cases: list[dict] = []
    if "golden" in which:
        golden = json.loads((EVALS_DIR / "golden_set.json").read_text(encoding="utf-8"))
        checks = json.loads((EVALS_DIR / "golden_checks.json").read_text(encoding="utf-8"))
        for g in sorted(golden, key=lambda c: c["id"]):
            cases.append({"id": g["id"], "set": "golden", "question": g["pergunta"], "expected": g["resposta_esperada"],
                          "expected_source": g.get("fonte_esperada", ""), "criteria": g.get("criterio_de_acerto", ""), "checks": checks.get(g["id"], {})})
    if "authorial" in which:
        for a in json.loads((EVALS_DIR / "authorial_cases.json").read_text(encoding="utf-8")):
            cases.append({"id": a["id"], "set": "authorial", "question": a["question"], "expected": a["expected"], "expected_source": "", "criteria": "", "checks": a.get("checks", {})})
    return cases


def evaluate(case: dict, outcome: dict) -> list[dict]:
    """Apply deterministic checks; each returns {name, passed, detail}."""
    checks = case.get("checks", {})
    answer = outcome.get("answer") or ""
    low = answer.lower()
    refused = outcome.get("status") == "refused"
    cited = outcome.get("citations") or []
    docs = {c["doc_code"] for c in cited if c.get("doc_code")}
    sqls = " ".join(t.get("sql", "") for t in outcome.get("tool_calls", []) if t.get("name") == "query_claims_db")
    results: list[dict] = []
    if "must_refuse" in checks:
        results.append({"name": "refusal", "passed": refused == checks["must_refuse"], "detail": f"status={outcome.get('status')}"})
    if not checks.get("must_refuse") and outcome.get("status") not in {"complete", "refused"}:
        results.append({"name": "answered", "passed": False, "detail": f"status={outcome.get('status')} error={outcome.get('error')}"})
    if checks.get("must_refuse") is not True:
        for group in checks.get("must_contain_any", []):
            ok = any(g.lower() in low for g in group)
            results.append({"name": f"contains any of {group}", "passed": ok, "detail": "" if ok else "not found"})
        for doc in checks.get("must_cite_doc", []):
            results.append({"name": f"cites {doc}", "passed": doc in docs, "detail": ", ".join(sorted(docs)) or "no citations"})
        if checks.get("must_cite_doc_any"):
            ok = any(d in docs for d in checks["must_cite_doc_any"])
            results.append({"name": f"cites any of {checks['must_cite_doc_any']}", "passed": ok, "detail": ", ".join(sorted(docs)) or "no citations"})
        for code, version in checks.get("must_cite_version", {}).items():
            ok = any(c.get("doc_code") == code and c.get("doc_version") == version for c in cited)
            results.append({"name": f"cites {code} v{version}", "passed": ok, "detail": ", ".join(f"{c.get('doc_code')} v{c.get('doc_version')}" for c in cited)})
        if checks.get("must_cite_database"):
            ok = any(c.get("source_type") == "database" for c in cited)
            results.append({"name": "cites a database query", "passed": ok, "detail": "" if ok else "no database citation"})
        for needle in checks.get("sql_must_contain", []):
            results.append({"name": f"SQL uses {needle}", "passed": needle in sqls, "detail": sqls[:120]})
        for rule in checks.get("forbid_unless", []):
            hit = re.search(rule["pattern"], answer) is not None
            excused = rule["unless"].lower() in low
            results.append({"name": f"no '{rule['pattern']}' unless '{rule['unless']}'", "passed": (not hit) or excused, "detail": "found" if hit else ""})
    for pattern in checks.get("must_not_match", []):
        hit = re.search(pattern, answer) is not None
        results.append({"name": f"never matches {pattern}", "passed": not hit, "detail": "LEAKED" if hit else ""})
    return results


async def run_case(case: dict, deps: Deps) -> dict:
    run = new_run(deps)
    started = time.perf_counter()
    outcome: dict = {"id": case["id"], "set": case["set"], "question": case["question"], "expected": case["expected"], "answer": "", "status": None,
                     "refusal_code": None, "citations": [], "tool_calls": [], "error": None}
    try:
        async for ev in answer_question(case["question"], [], deps, run):
            if isinstance(ev, Completed):
                outcome.update(answer=ev.content, status=ev.status, refusal_code=ev.refusal_code,
                               citations=[asdict(c) for c in ev.citations], provenance=ev.provenance)
    except AppError as err:
        outcome.update(status="error", error=err.code, answer=run.partial_text)
    outcome["tool_calls"] = [{"name": s.kind, "sql": s.query.sql} for s in run.registry.all() if s.kind == "database"]
    outcome["tool_calls"] = [{"name": "query_claims_db", "sql": t["sql"]} for t in outcome["tool_calls"]]
    outcome["latency_ms"] = int((time.perf_counter() - started) * 1000)
    m = run.metrics()
    outcome["metrics"] = {"input_tokens": m.input_tokens, "output_tokens": m.output_tokens, "cost_usd": m.cost_usd, "llm_calls": m.llm_calls,
                          "retries": m.retries, "tool_calls": m.tool_calls, "pii_hits": m.pii_hits, "served_model": m.served_model}
    outcome["checks"] = evaluate(case, outcome)
    outcome["passed"] = all(c["passed"] for c in outcome["checks"]) and outcome["status"] in {"complete", "refused"}
    outcome["ran_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return outcome


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p * (len(s) - 1))))
    return s[k]


def render_report(results: list[dict], settings: Settings, index_info: dict, db_label: str, dry_run: bool) -> str:
    lines = []
    ran = [r for r in results if r.get("status")]
    lat = [r["latency_ms"] for r in ran]
    cost = [r["metrics"]["cost_usd"] for r in ran]
    tokens = [r["metrics"]["input_tokens"] + r["metrics"]["output_tokens"] for r in ran]
    passed = sum(1 for r in ran if r["passed"])
    lines.append(f"Run: {datetime.now(timezone.utc).isoformat(timespec='seconds')} · model: `{'fake (dry run)' if dry_run else settings.llm_model}` · fallbacks: `{settings.llm_fallback_models or '-'}` · reasoning effort: `{settings.llm_reasoning_effort or '-'}`  ")
    lines.append(f"Index built: {index_info.get('built_at')} ({index_info.get('documents')} docs, {index_info.get('chunks')} chunks) · claims DB: {db_label}  ")
    lines.append(f"Prices for the equivalent cost: US$ {settings.llm_input_price_per_m}/M input, US$ {settings.llm_output_price_per_m}/M output")
    lines.append("")
    lines.append("| id | question | expected | result | cited | tools | calls | tokens in/out | cost US$ | latency | pass |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        cited = ", ".join(sorted({(c.get("doc_code") or "DB") + (f" v{c['doc_version']}" if c.get("doc_version") else "") for c in r.get("citations", [])})) or "—"
        tools = ", ".join(sorted({t["name"] for t in r.get("tool_calls", [])})) or "—"
        status = r.get("status") or "not run"
        result = f"{status}" + (f" ({r['refusal_code']})" if r.get("refusal_code") else "") + (f" [{r['error']}]" if r.get("error") else "")
        answer = (r.get("answer") or "").replace("\n", " ").replace("|", "\\|")
        m = r.get("metrics", {})
        lines.append(
            f"| {r['id']} | {r['question'][:70].replace('|', '/')}{'…' if len(r['question']) > 70 else ''} | {r['expected'][:60].replace('|', '/')}{'…' if len(r['expected']) > 60 else ''} "
            f"| {result}: {answer[:110]}{'…' if len(answer) > 110 else ''} | {cited} | {tools} | {m.get('llm_calls', 0)} | {m.get('input_tokens', 0)}/{m.get('output_tokens', 0)} "
            f"| {m.get('cost_usd', 0):.4f} | {r.get('latency_ms', 0) / 1000:.1f} s | {'✅' if r.get('passed') else '❌'} |"
        )
    lines.append("")
    lines.append("**Aggregates**")
    lines.append("")
    golden = [r for r in ran if r["set"] == "golden"]
    auth = [r for r in ran if r["set"] == "authorial"]
    lines.append(f"- Golden set: {sum(1 for r in golden if r['passed'])}/{len(golden)} passed · authorial: {sum(1 for r in auth if r['passed'])}/{len(auth)} passed · total {passed}/{len(ran)}")
    exp_ref = [r for r in ran if any(c['name'] == 'refusal' for c in r['checks'])]
    lines.append(f"- Refusal behaviour: {sum(1 for r in exp_ref if all(c['passed'] for c in r['checks'] if c['name'] == 'refusal'))}/{len(exp_ref)} cases refused/answered as expected")
    leaks = sum(1 for r in ran for c in r["checks"] if c["name"].startswith("never matches") and not c["passed"])
    lines.append(f"- PII leaks detected by regex/name checks: {leaks}")
    lines.append(f"- Latency: p50 {percentile(lat, 0.5) / 1000:.1f} s · p95 {percentile(lat, 0.95) / 1000:.1f} s · max {max(lat, default=0) / 1000:.1f} s")
    lines.append(f"- Tokens per question: mean {statistics.mean(tokens) if tokens else 0:.0f} · LLM calls per question: mean {statistics.mean([r['metrics']['llm_calls'] for r in ran]) if ran else 0:.2f}")
    lines.append(f"- Equivalent cost per question: mean US$ {statistics.mean(cost) if cost else 0:.4f} · max US$ {max(cost, default=0):.4f} (budget US$ {settings.max_cost_usd_per_question})")
    failed = [r for r in ran if not r["passed"]]
    if failed:
        lines.append("")
        lines.append("**Failed checks**")
        lines.append("")
        for r in failed:
            for c in r["checks"]:
                if not c["passed"]:
                    lines.append(f"- {r['id']}: {c['name']} — {c['detail']}")
    return "\n".join(lines) + "\n"


def dry_run_router():
    from app.llm.fake_provider import answer, refuse, tool_call

    def router(req):
        last = req.messages[-1]
        content = last.get("content") or ""
        if last["role"] == "tool":
            n = re.search(r"fonte \[(\d+)\]", content)
            return answer(f"Conforme o banco de sinistros [{n.group(1) if n else 1}] e a tabela de coberturas [1].")
        q = content.split("PERGUNTA DO ANALISTA:", 1)[-1].split("\n\n", 1)[0].lower()
        if any(w in q for w in ("cpf", "nome", "telefone")):
            return refuse("PII_REQUEST", "Não posso expor dados pessoais (POL-LGPD-2024).")
        if any(w in q for w in ("desconto", "iof", "bom dia")):
            return refuse("NO_SOURCE", "Não há fonte para isso.")
        sin_match = re.search(r"sin-\d{4}-\d{6}", q)
        if sin_match and req.tool_choice != "none":
            sin = sin_match.group(0).upper()
            return tool_call("query_claims_db", {"sql": f"SELECT c.claim_amount, p.paid_amount FROM claims c JOIN payments p ON p.claim_id=c.claim_id WHERE c.claim_number='{sin}'", "purpose": "x"})
        return answer("Resposta simulada [1].")

    return router


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="golden,authorial")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--pause", type=float, default=3.5, help="seconds between cases (free tier: 20 req/min)")
    ap.add_argument("--fresh", action="store_true", help="ignore saved results and re-run every case")
    ap.add_argument("--dry-run", action="store_true", help="use the scripted fake provider")
    ap.add_argument("--report", default=str(RESULTS_DIR / "report.md"))
    args = ap.parse_args(argv)

    settings = Settings()
    if not Path(settings.index_db_path).exists():
        build_index(settings.corpus_dir, settings.index_db_path, settings.claims_db_path)
    retriever = Retriever(settings.index_db_path)
    claims = ClaimsDB(settings.claims_db_path)
    scrubber = PIIScrubber(load_policyholder_names(settings.claims_db_path))
    if args.dry_run:
        from app.llm.fake_provider import FakeProvider

        provider = FakeProvider(router=dry_run_router())
    else:
        if not settings.llm_api_key:
            print("LLM_API_KEY missing in .env (use --dry-run for a harness check)", file=sys.stderr)
            return 2
        from app.llm.openai_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(settings)
    deps = Deps(settings=settings, llm=ResilientLLM(provider, settings), retriever=retriever, claims_db=claims, scrubber=scrubber)

    results_dir = RESULTS_DIR / ("dry-run" if args.dry_run else "latest")
    results_dir.mkdir(parents=True, exist_ok=True)
    cases = load_cases(set(args.cases.split(",")))
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    results: list[dict] = []
    for i, case in enumerate(cases):
        path = results_dir / f"{case['id']}.json"
        if path.exists() and not args.fresh and not args.only:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("status") in {"complete", "refused"}:
                results.append(saved)
                print(f"{case['id']}: cached ({'PASS' if saved['passed'] else 'FAIL'})")
                continue
        outcome = await run_case(case, deps)
        path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(outcome)
        print(f"{case['id']}: {outcome['status']} {'PASS' if outcome['passed'] else 'FAIL'} · {outcome['latency_ms']} ms · calls={outcome['metrics']['llm_calls']} · {outcome['answer'][:90]!r}")
        if outcome.get("error") in {"LLM_RATE_LIMITED", "LLM_UNAVAILABLE"} and not args.dry_run:
            print("provider rate limited/unavailable: pausing 30 s before the next case")
            await asyncio.sleep(30)
        elif i < len(cases) - 1 and not args.dry_run:
            await asyncio.sleep(args.pause)
    report = render_report(results, settings, retriever.info(), claims.snapshot_label(), args.dry_run)
    Path(args.report).write_text(report, encoding="utf-8")
    print(f"\nreport written to {args.report}")
    print(report.split("**Aggregates**")[1] if "**Aggregates**" in report else report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
