import asyncio

import pytest

from app.core.config import Settings
from app.core.errors import BudgetExceeded, LLMMisconfigured, LLMRateLimited, LLMTimeout, LLMUnavailable
from app.llm.fake_provider import Fail, FakeProvider, Hang, answer
from app.llm.resilient import CircuitBreaker, ResilientLLM
from app.llm.types import LLMRequest, MessageEnd, TextDelta, Usage


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(provider, clock, **overrides):
    kwargs = dict(llm_api_key="k", question_deadline_s=30, llm_max_retries=2, cb_failure_threshold=3, cb_open_s=30)
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        clock.t += s

    llm = ResilientLLM(provider, settings, sleep=sleep, clock=clock, rng=lambda: 0.0)
    return llm, sleeps


async def run(llm, budget):
    return [ev async for ev in llm.stream_turn(LLMRequest(system="s", messages=[]), budget)]


async def test_retries_then_succeeds_and_counts():
    clock = FakeClock()
    provider = FakeProvider([Fail(LLMUnavailable()), Fail(LLMTimeout()), answer("ok [1]")])
    llm, sleeps = make(provider, clock)
    budget = llm.new_budget()
    events = await run(llm, budget)
    assert isinstance(events[-1], MessageEnd) and len(provider.calls) == 3
    assert budget.retries == 2 and budget.llm_calls == 1
    assert sleeps == [0.5, 1.0]  # exponential backoff, no jitter with rng=0


async def test_retry_limit_is_respected():
    clock = FakeClock()
    provider = FakeProvider([Fail(LLMUnavailable()), Fail(LLMUnavailable()), Fail(LLMUnavailable()), answer("never")])
    llm, _ = make(provider, clock)
    with pytest.raises(LLMUnavailable):
        await run(llm, llm.new_budget())
    assert len(provider.calls) == 3  # 1 + max_retries


async def test_non_retryable_error_is_not_retried():
    clock = FakeClock()
    provider = FakeProvider([Fail(LLMMisconfigured()), answer("never")])
    llm, _ = make(provider, clock)
    with pytest.raises(LLMMisconfigured):
        await run(llm, llm.new_budget())
    assert len(provider.calls) == 1


async def test_mid_stream_failure_is_not_retried_and_marked_partial():
    clock = FakeClock()
    provider = FakeProvider([Fail(LLMUnavailable(), after_text="parte da resposta"), answer("never")])
    llm, _ = make(provider, clock)
    got = []
    with pytest.raises(LLMUnavailable) as exc:
        async for ev in llm.stream_turn(LLMRequest(system="s", messages=[]), llm.new_budget()):
            got.append(ev)
    assert exc.value.partial is True and isinstance(got[0], TextDelta) and len(provider.calls) == 1


async def test_rate_limit_honours_retry_after_or_fails_fast():
    clock = FakeClock()
    provider = FakeProvider([Fail(LLMRateLimited(retry_after_s=4)), answer("ok")])
    llm, sleeps = make(provider, clock)
    await run(llm, llm.new_budget())
    assert sleeps == [4]
    # retry-after larger than the remaining deadline -> fail fast, keep the wait for the UI
    provider = FakeProvider([Fail(LLMRateLimited(retry_after_s=60)), answer("ok")])
    llm, sleeps = make(provider, clock)
    with pytest.raises(LLMRateLimited) as exc:
        await run(llm, llm.new_budget())
    assert exc.value.retry_after_s == 60 and sleeps == [] and len(provider.calls) == 1


async def test_deadline_interrupts_a_hanging_provider():
    clock = FakeClock()
    provider = FakeProvider([Hang(3600)])
    llm, _ = make(provider, clock, question_deadline_s=0.05)
    with pytest.raises(LLMTimeout):
        await run(llm, llm.new_budget())


async def test_circuit_breaker_opens_short_circuits_and_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=3, open_s=30, clock=clock)
    provider = FakeProvider([Fail(LLMRateLimited()), Fail(LLMRateLimited()), Fail(LLMUnavailable()), answer("ok")])
    settings = Settings(llm_api_key="k", llm_max_retries=0, question_deadline_s=30)
    llm = ResilientLLM(provider, settings, breaker=breaker, clock=clock, rng=lambda: 0.0)
    for _ in range(3):
        with pytest.raises((LLMRateLimited, LLMUnavailable)):
            await run(llm, llm.new_budget())
    assert breaker.status()["circuit"] == "open" and breaker.status()["reason"] == "rate_limited"
    with pytest.raises(LLMRateLimited) as exc:  # short-circuited: provider not called
        await run(llm, llm.new_budget())
    assert len(provider.calls) == 3 and exc.value.retry_after_s > 0
    clock.t += 31  # half-open probe succeeds
    assert breaker.status()["circuit"] == "half_open"
    events = await run(llm, llm.new_budget())
    assert isinstance(events[-1], MessageEnd) and breaker.status()["circuit"] == "closed"


async def test_budget_caps_block_further_calls():
    clock = FakeClock()
    provider = FakeProvider([answer("ok", usage=Usage(5000, 500))])
    llm, _ = make(provider, clock, max_tokens_per_question=6000, llm_input_price_per_m=2.0, llm_output_price_per_m=10.0)
    budget = llm.new_budget()
    await run(llm, budget)
    assert budget.total_tokens == 5500 and round(budget.cost_usd, 4) == 0.015
    with pytest.raises(BudgetExceeded):
        await run(llm, budget)  # a second call like the first would exceed the token cap


async def test_semaphore_serialises_calls():
    clock = FakeClock()
    active = 0
    peak = 0

    async def slow_turn(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        for ev in answer("ok").events:
            yield ev

    class P:
        name = "p"
        stream_turn = staticmethod(slow_turn)

    settings = Settings(llm_api_key="k", llm_max_concurrency=1, question_deadline_s=30)
    llm = ResilientLLM(P(), settings, clock=clock)
    await asyncio.gather(*(run(llm, llm.new_budget()) for _ in range(3)))
    assert peak == 1
