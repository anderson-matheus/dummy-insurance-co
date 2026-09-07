"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import routes_conversations, routes_health, routes_messages
from app.api.deps import AppState
from app.chat.orchestrator import Deps
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, correlation_id, new_correlation_id
from app.history.repo import HistoryRepo
from app.knowledge.claims_db import ClaimsDB
from app.knowledge.ingest import build_index
from app.knowledge.pii import PIIScrubber, load_policyholder_names
from app.knowledge.retriever import Retriever
from app.llm.resilient import ResilientLLM
from app.runs import RunRegistry

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider=None,
    retriever: Retriever | None = None,
    claims_db: ClaimsDB | None = None,
    repo: HistoryRepo | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal retriever, claims_db, repo, provider
        repo = repo or HistoryRepo(settings.history_db_path)
        await repo.connect()
        swept = await repo.sweep_pending()
        if swept:
            log.warning("marked %d pending replies as interrupted after restart", swept)
        if retriever is None:
            if not Path(settings.index_db_path).exists():
                log.info("index missing, building from %s", settings.corpus_dir)
                await asyncio.to_thread(build_index, settings.corpus_dir, settings.index_db_path, settings.claims_db_path, False, log.info)
            retriever = Retriever(settings.index_db_path)
        claims_db = claims_db or ClaimsDB(settings.claims_db_path)
        scrubber = PIIScrubber(load_policyholder_names(settings.claims_db_path))
        if provider is None:
            from app.llm.openai_provider import OpenAICompatProvider

            provider = OpenAICompatProvider(settings)
        llm = ResilientLLM(provider, settings)
        deps = Deps(settings=settings, llm=llm, retriever=retriever, claims_db=claims_db, scrubber=scrubber)
        runs = RunRegistry()
        app.state.ctx = AppState(settings=settings, repo=repo, llm=llm, deps=deps, runs=runs)
        log.info("ready: %s", retriever.info())
        try:
            yield
        finally:
            await runs.shutdown()
            await repo.close()
            retriever.close()

    app = FastAPI(title=settings.app_title, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = new_correlation_id()
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        log.warning("%s: %s", exc.code, exc.detail)
        return JSONResponse(status_code=exc.http_status, content=exc.to_envelope(correlation_id.get()))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError):
        err = AppError(detail=str(exc), code="VALIDATION", http_status=422)
        return JSONResponse(status_code=422, content=err.to_envelope(correlation_id.get()))

    @app.exception_handler(Exception)
    async def unexpected_handler(_request: Request, exc: Exception):
        log.exception("unhandled error")
        err = AppError(detail=str(exc))
        return JSONResponse(status_code=500, content=err.to_envelope(correlation_id.get()))

    app.include_router(routes_health.router, prefix="/api")
    app.include_router(routes_conversations.router, prefix="/api")
    app.include_router(routes_messages.router, prefix="/api")
    return app


app = create_app()
