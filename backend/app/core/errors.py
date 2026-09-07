"""Error taxonomy shared by the LLM layer, orchestrator and API.

User-facing messages are fixed pt-BR strings that never expose internals
(stack traces, provider bodies, model names, prompts). Internal details are
kept in ``detail`` for logging only.
"""
from __future__ import annotations

from typing import Any

# code -> (user message, retryable)
ERROR_CATALOG: dict[str, tuple[str, bool]] = {
    "LLM_TIMEOUT": (
        "O assistente não respondeu dentro do tempo limite. Você pode tentar novamente.",
        True,
    ),
    "LLM_RATE_LIMITED": (
        "O provedor de IA atingiu o limite de uso. Aguarde alguns instantes e tente novamente.",
        True,
    ),
    "LLM_UNAVAILABLE": (
        "O assistente está temporariamente indisponível. O histórico continua acessível; "
        "tente novamente em instantes.",
        True,
    ),
    "LLM_BAD_RESPONSE": (
        "O assistente retornou uma resposta inválida. Tente novamente.",
        True,
    ),
    "LLM_MISCONFIGURED": (
        "O assistente não está configurado corretamente. Contate o administrador.",
        False,
    ),
    "BUDGET_EXCEEDED": (
        "A pergunta excedeu o limite de processamento por consulta. "
        "Tente uma pergunta mais específica.",
        False,
    ),
    "RESPONSE_TRUNCATED": (
        "A resposta foi interrompida por exceder o tamanho máximo. Tente novamente.",
        True,
    ),
    "CANCELLED": ("Resposta cancelada pelo usuário.", True),
    "SERVER_RESTART": (
        "A geração desta resposta foi interrompida por uma reinicialização do serviço.",
        True,
    ),
    "INTERNAL": (
        "Ocorreu um erro interno. Tente novamente; se persistir, informe o código de referência.",
        True,
    ),
    "NOT_FOUND": ("Recurso não encontrado.", False),
    "IDEMPOTENCY_CONFLICT": (
        "Já existe uma mensagem com este identificador e conteúdo diferente.",
        False,
    ),
    "IN_PROGRESS": ("Esta pergunta ainda está sendo processada.", False),
    "VALIDATION": ("Requisição inválida.", False),
}


def describe_error(code: str) -> tuple[str, bool]:
    return ERROR_CATALOG.get(code, ERROR_CATALOG["INTERNAL"])


class AppError(Exception):
    code = "INTERNAL"
    http_status = 500

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.user_message, self.retryable = describe_error(self.code)
        self.retry_after_s = retry_after_s
        self.detail = detail  # internal only
        super().__init__(detail or self.code)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.user_message,
            "retryable": self.retryable,
        }
        if self.retry_after_s is not None:
            out["retry_after_s"] = round(self.retry_after_s, 1)
        return out

    def to_envelope(self, correlation_id: str) -> dict[str, Any]:
        return {"error": {**self.to_dict(), "correlation_id": correlation_id}}


class LLMError(AppError):
    """Base for provider failures. ``partial`` is True when text had already streamed."""

    partial = False


class LLMTimeout(LLMError):
    code = "LLM_TIMEOUT"
    http_status = 504


class LLMRateLimited(LLMError):
    code = "LLM_RATE_LIMITED"
    http_status = 503


class LLMUnavailable(LLMError):
    code = "LLM_UNAVAILABLE"
    http_status = 503


class LLMBadResponse(LLMError):
    code = "LLM_BAD_RESPONSE"
    http_status = 502


class LLMMisconfigured(LLMError):
    code = "LLM_MISCONFIGURED"
    http_status = 503


class BudgetExceeded(AppError):
    code = "BUDGET_EXCEEDED"
    http_status = 422


class ResponseTruncated(AppError):
    code = "RESPONSE_TRUNCATED"
    http_status = 502


class Cancelled(AppError):
    code = "CANCELLED"
    http_status = 499


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404


class Conflict(AppError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409
