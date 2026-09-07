"""Detached generation runs with fan-out to SSE subscribers.

A question is answered by a background task so that a dropped browser
connection does not lose a finished answer; the API endpoints subscribe to the
run's event feed (late subscribers get a replay) and ``cancel`` stops it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

HEARTBEAT_S = 15.0


@dataclass
class Run:
    message_id: str
    task: asyncio.Task | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    done: bool = False
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        async with self._cond:
            self.events.append((event, data))
            self._cond.notify_all()

    async def finish(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def wait(self) -> None:
        async with self._cond:
            while not self.done:
                await self._cond.wait()

    async def subscribe(self) -> AsyncIterator[tuple[str, dict[str, Any]] | None]:
        """Yield events (replaying history); ``None`` marks an idle heartbeat tick."""
        i = 0
        while True:
            async with self._cond:
                while i >= len(self.events) and not self.done:
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        break
                if i < len(self.events):
                    ev = self.events[i]
                    i += 1
                elif self.done:
                    return
                else:
                    ev = None
            yield ev


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def get(self, message_id: str) -> Run | None:
        return self._runs.get(message_id)

    def start(self, message_id: str, worker: Callable[[Run], Awaitable[None]]) -> Run:
        run = Run(message_id=message_id)
        self._runs[message_id] = run

        async def _wrapped() -> None:
            try:
                await worker(run)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker persists its own failures; this is the last resort
                log.exception("run %s crashed", message_id)
            finally:
                await run.finish()
                self._runs.pop(message_id, None)

        run.task = asyncio.create_task(_wrapped(), name=f"run-{message_id}")
        return run

    def cancel(self, message_id: str) -> bool:
        run = self._runs.get(message_id)
        if run is None or run.task is None or run.done:
            return False
        run.task.cancel()
        return True

    async def shutdown(self) -> None:
        for run in list(self._runs.values()):
            if run.task is not None and not run.task.done():
                run.task.cancel()
        for run in list(self._runs.values()):
            if run.task is not None:
                try:
                    await run.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
