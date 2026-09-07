from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.chat.orchestrator import Deps
from app.core.config import Settings
from app.history.repo import HistoryRepo
from app.llm.resilient import ResilientLLM
from app.runs import RunRegistry


@dataclass
class AppState:
    settings: Settings
    repo: HistoryRepo
    llm: ResilientLLM
    deps: Deps
    runs: RunRegistry


def get_state(request: Request) -> AppState:
    return request.app.state.ctx
