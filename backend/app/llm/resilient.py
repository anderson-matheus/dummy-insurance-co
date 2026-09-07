"""Resilience around any ``LLMProvider``: deadline, bounded retries, circuit breaker, budget.

Policy (see DECISIONS.md):
- retries only for retryable errors raised before any text reached the user
  (a mid-stream failure is surfaced as an interrupted answer with "try again");
- 429 honours Retry-After when it fits the remaining deadline, otherwise the
  question fails fast with the wait time so the UI can show a countdown;
- the breaker opens after N consecutive failures and short-circuits calls, so
  a dead provider costs nothing and the app stays responsive;
- the per-question budget caps both list-price cost and tokens; free tiers
  bill nothing but quota is finite.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from app.core.config import Settings
from app.core.errors import BudgetExceeded, LLMError, LLMRateLimited, LLMTimeout, LLMUnavailable
from app.llm.types import LLMEvent, LLMRequest, MessageEnd, TextDelta, Usage

log = logging.getLogger(__name__)

MIN_USEFUL_CALL_S = 5.0


class CircuitBreaker:
    def __init__(self, threshold: int = 3, open_s: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.open_s = open_s
        self.clock = clock
        self.state = "closed"
        self.failures = 0
        self.open_until = 0.0
        self.reason: str | None = None
        self._recent: deque[str] = deque(maxlen=threshold)
        self._probe_in_flight = False

    def allow(self) -> bool:
        now = self.clock()
        if self.state == "open":
            if now < self.open_until:
                return False
            self.state = "half_open"
            self._probe_in_flight = False
        if self.state == "half_open":
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.reason = None
        self._recent.clear()
        self._probe_in_flight = False

    def record_failure(self, code: str, retry_after_s: float | None = None) -> None:
        self.failures += 1
        self._recent.append(code)
        self._probe_in_flight = False
        if self.state == "half_open" or self.failures >= self.threshold:
            self.state = "open"
            rate_limited = sum(1 for c in self._recent if c == "LLM_RATE_LIMITED")
            self.reason = "rate_limited" if rate_limited * 2 >= len(self._recent) else "unavailable"
            wait = max(self.open_s, retry_after_s or 0.0)
            self.open_until = self.clock() + wait
            log.warning("circuit opened (%s) for %.0fs after %d failures", self.reason, wait, self.failures)

    def status(self) -> dict:
        now = self.clock()
        if self.state == "open" and now >= self.open_until:
            state = "half_open"
        else:
            state = self.state
        return {
            "circuit": state,
            "reason": self.reason if state != "closed" else None,
            "open_for_s": round(max(0.0, self.open_until - now), 1) if state == "open" else 0.0,
            "consecutive_failures": self.failures,
        }

    def short_circuit_error(self) -> LLMError:
        wait = max(0.0, self.open_until - self.clock())
        if self.reason == "rate_limited":
            return LLMRateLimited(detail="circuit open (rate limited)", retry_after_s=wait)
        return LLMUnavailable(detail="circuit open (unavailable)", retry_after_s=wait)


@dataclass
class QuestionBudget:
    """Per-question accounting and caps (cost is list-price equivalent)."""

    deadline: float
    max_cost_usd: float
    max_tokens: int
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    retries: int = 0
    request_ids: list[str] = field(default_factory=list)
    served_models: list[str] = field(default_factory=list)
    _last_call_cost: float = 0.0
    _last_call_tokens: int = 0

    def add(self, end: MessageEnd) -> None:
        u: Usage = end.usage
        cost = (u.input_tokens * self.price_in_per_m + u.output_tokens * self.price_out_per_m) / 1e6
        self.cost_usd += cost
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.llm_calls += 1
        self._last_call_cost = cost
        self._last_call_tokens = u.total
        if end.request_id:
            self.request_ids.append(end.request_id)
        if end.served_model and end.served_model not in self.served_models:
            self.served_models.append(end.served_model)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check_before_call(self) -> None:
        """Raise if another call like the last one would exceed a cap."""
        if self.cost_usd + self._last_call_cost > self.max_cost_usd and self.max_cost_usd > 0:
            raise BudgetExceeded(detail=f"cost {self.cost_usd:.4f} + {self._last_call_cost:.4f} > {self.max_cost_usd}")
        if self.total_tokens + self._last_call_tokens > self.max_tokens and self.max_tokens > 0:
            raise BudgetExceeded(detail=f"tokens {self.total_tokens} + {self._last_call_tokens} > {self.max_tokens}")

    def remaining_s(self, clock: Callable[[], float] = time.monotonic) -> float:
        return self.deadline - clock()


class ResilientLLM:
    def __init__(
        self,
        provider,
        settings: Settings,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.breaker = breaker or CircuitBreaker(settings.cb_failure_threshold, settings.cb_open_s, clock)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))

    def new_budget(self) -> QuestionBudget:
        s = self.settings
        return QuestionBudget(
            deadline=self._clock() + s.question_deadline_s,
            max_cost_usd=s.max_cost_usd_per_question,
            max_tokens=s.max_tokens_per_question,
            price_in_per_m=s.llm_input_price_per_m,
            price_out_per_m=s.llm_output_price_per_m,
        )

    def _backoff(self, attempt: int, err: LLMError) -> float:
        if isinstance(err, LLMRateLimited):
            if err.retry_after_s is not None:
                return err.retry_after_s
            return min(2.0 * (2**attempt) + self._rng(), 8.0)
        return min(0.5 * (2**attempt) + 0.3 * self._rng(), 4.0)

    async def stream_turn(self, req: LLMRequest, budget: QuestionBudget) -> AsyncIterator[LLMEvent]:
        budget.check_before_call()
        attempt = 0
        while True:
            if not self.breaker.allow():
                raise self.breaker.short_circuit_error()
            started_stream = False
            try:
                async with self._semaphore:
                    agen = self.provider.stream_turn(req)
                    try:
                        while True:
                            remaining = budget.remaining_s(self._clock)
                            if remaining <= 0:
                                raise LLMTimeout(detail="question deadline reached")
                            try:
                                ev = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError as exc:
                                raise LLMTimeout(detail="deadline while waiting for provider") from exc
                            if isinstance(ev, TextDelta):
                                started_stream = True
                            if isinstance(ev, MessageEnd):
                                budget.add(ev)
                                self.breaker.record_success()
                            yield ev
                    finally:
                        await agen.aclose()
                return
            except LLMError as err:
                err.partial = started_stream
                self.breaker.record_failure(err.code, err.retry_after_s)
                if started_stream or not err.retryable or attempt >= self.settings.llm_max_retries:
                    raise
                delay = self._backoff(attempt, err)
                if budget.remaining_s(self._clock) < delay + MIN_USEFUL_CALL_S:
                    log.info("not retrying %s: %.1fs backoff does not fit the deadline", err.code, delay)
                    raise
                attempt += 1
                budget.retries += 1
                log.info("retrying after %s (attempt %d, backoff %.1fs)", err.code, attempt, delay)
                await self._sleep(delay)
