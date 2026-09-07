from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AppState, get_state

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(state: AppState = Depends(get_state)) -> dict:
    breaker = state.llm.breaker.status()
    try:
        index = state.deps.retriever.info()
    except Exception:  # noqa: BLE001
        index = {"built_at": None, "documents": 0, "chunks": 0}
    try:
        claims = state.deps.claims_db.snapshot_info()
    except Exception:  # noqa: BLE001
        claims = {}
    degraded = breaker["circuit"] != "closed" or not index.get("chunks") or not state.settings.llm_api_key
    return {
        "status": "degraded" if degraded else "ok",
        "provider": {**breaker, "configured": bool(state.settings.llm_api_key)},
        "index": index,
        "claims_db": claims,
        "history_db": "ok",
    }
