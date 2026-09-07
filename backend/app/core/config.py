"""Application settings (environment-driven, pydantic-settings)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"),), env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM provider (any OpenAI-compatible Chat Completions endpoint) ---
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    llm_fallback_models: str = ""  # comma-separated OpenRouter fallbacks (max 3)
    llm_reasoning_effort: str = "none"  # none|low|medium|high -> OpenRouter "reasoning" param; "" = not sent
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 700
    llm_input_price_per_m: float = 0.0  # US$ per 1M tokens, for equivalent-cost reporting
    llm_output_price_per_m: float = 0.0

    # --- Resilience ---
    llm_timeout_s: float = 15.0
    llm_connect_timeout_s: float = 3.0
    llm_max_retries: int = 2
    llm_max_concurrency: int = 2
    question_deadline_s: float = 30.0
    max_tool_iterations: int = 4
    max_tool_repairs: int = 2
    max_cost_usd_per_question: float = 0.05
    max_tokens_per_question: int = 16000
    cb_failure_threshold: int = 3
    cb_open_s: float = 30.0

    # --- Retrieval ---
    retrieval_top_k: int = 8
    tool_search_top_k: int = 4
    max_search_calls: int = 2  # extra document searches per question (the prompt asks for at most two)
    history_turns: int = 4

    # --- Paths ---
    corpus_dir: str = str(REPO_ROOT / "data" / "corpus")
    claims_db_path: str = str(REPO_ROOT / "data" / "claims.db")
    history_db_path: str = str(REPO_ROOT / "var" / "history.db")
    index_db_path: str = str(REPO_ROOT / "var" / "index.db")

    log_level: str = "info"
    app_title: str = "Assistente de Sinistros"

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()][:3]


def get_settings() -> Settings:
    return Settings()
