"""Tool execution for the orchestrator (argument validation, guards, source registration)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.chat.sources import Source, SourceRegistry
from app.knowledge.claims_db import ClaimsDB, SQLGuardError
from app.knowledge.retriever import Retriever
from app.llm.types import ToolUse

log = logging.getLogger(__name__)

REFUSAL_CODES = {"NO_SOURCE", "PII_REQUEST", "OUT_OF_SCOPE", "AMBIGUOUS"}


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    product: str | None = None


class QueryArgs(BaseModel):
    sql: str = Field(min_length=6, max_length=2000)
    purpose: str = ""


class RefuseArgs(BaseModel):
    reason_code: str = "NO_SOURCE"
    message: str = Field(min_length=1, max_length=1000)


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    new_sources: list[Source] = field(default_factory=list)
    refusal: RefuseArgs | None = None
    stage: str | None = None


def _error(msg: str, expected: str) -> ToolOutcome:
    return ToolOutcome(content=json.dumps({"error": msg, "expected": expected}, ensure_ascii=False), is_error=True)


async def execute_tool(
    tool: ToolUse, registry: SourceRegistry, retriever: Retriever, claims_db: ClaimsDB, search_k: int = 5
) -> ToolOutcome:
    if tool.input is None:
        return _error(f"argumentos inválidos ({tool.parse_error})", "um objeto JSON válido com os parâmetros da ferramenta")
    try:
        if tool.name == "search_documents":
            args = SearchArgs(**tool.input)
            product = args.product if args.product in {"Auto", "Residencial", "Empresarial"} else None
            hits = retriever.search(args.query, k=search_k, product=product)
            new: list[Source] = []
            for chunk in hits:
                src, created = registry.add_chunk(chunk)
                if created:
                    new.append(src)
            if not hits:
                return ToolOutcome(content="Nenhum trecho encontrado para esses termos. Tente outros termos ou chame refuse.", stage="searching_documents")
            already = [s.n for s in (registry.get(registry.add_chunk(c)[0].n) for c in hits) if s not in new]
            text = registry.render_for_model(new) if new else ""
            note = f"(as fontes {', '.join(f'[{n}]' for n in already)} já estavam disponíveis)" if already else ""
            return ToolOutcome(content=f"NOVAS FONTES:\n{text}\n{note}".strip(), new_sources=new, stage="searching_documents")
        if tool.name == "query_claims_db":
            args = QueryArgs(**tool.input)
            try:
                result = await claims_db.query(args.sql)
            except SQLGuardError as exc:
                return ToolOutcome(content=json.dumps({"error": str(exc), "expected": "uma única consulta SELECT sem colunas de dados pessoais"}, ensure_ascii=False), is_error=True, stage="querying_db")
            src, created = registry.add_query(result)
            return ToolOutcome(content=f"RESULTADO DA CONSULTA (fonte [{src.n}]):\n{result.as_text()}", new_sources=[src] if created else [], stage="querying_db")
        if tool.name == "refuse":
            args = RefuseArgs(**tool.input)
            code = args.reason_code.strip().upper()
            if code not in REFUSAL_CODES:
                code = "NO_SOURCE"
            return ToolOutcome(content="ok", refusal=RefuseArgs(reason_code=code, message=args.message))
        return _error(f"ferramenta desconhecida: {tool.name}", "search_documents, query_claims_db ou refuse")
    except ValidationError as exc:
        return _error("argumentos inválidos: " + "; ".join(e.get("msg", "") for e in exc.errors()), "os parâmetros descritos na definição da ferramenta")
