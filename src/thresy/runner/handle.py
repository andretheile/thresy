"""RunHandle: the async iterator every runner's run() returns.

A producer task drives the run and puts events into a queue, so the timeout is
enforced even while the consumer is busy. The handle guarantees: at most one
Started, and exactly one Final as the last event, unless the consuming task
itself is cancelled (then the run is stopped and CancelledError propagates,
because nobody is left to receive a Final).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from .core import Final, Progress, RunEvent, RunRequest, RunResult, Started

log = logging.getLogger(__name__)

# Producer tasks are referenced here so an abandoned handle cannot get its task
# garbage-collected mid-run (asyncio itself only keeps weak references).
_RUNNING: set[asyncio.Task] = set()


class RunContext:
    """What a driver gets: a way to emit events and to notice cancellation."""

    def __init__(self, handle: RunHandle) -> None:
        self._handle = handle
        self.cancel_event = asyncio.Event()
        self.cancel_reason: str | None = None
        self.session_id: str | None = None

    def emit(self, event: Started | Progress) -> None:
        if isinstance(event, Started):
            if self._handle._started_sent:
                if event.session_id and event.session_id != self.session_id:
                    log.warning("ignoring second session id %s", event.session_id)
                return
            self._handle._started_sent = True
            self.session_id = event.session_id
        elif not isinstance(event, Progress):
            raise TypeError(f"drivers emit Started or Progress, not {event!r}")
        self._handle._queue.put_nowait(event)


Driver = Callable[[RunContext], Awaitable[RunResult]]


class RunHandle:
    """Events of one run. Iterate with `async for`; optionally use as an async
    context manager so leaving the block early stops the run::

        async with runner.run(req) as run:
            async for event in run:
                ...

    `cancel(reason)` stops the run cooperatively; the iterator then still ends
    with a Final (error_kind "cancelled", error_detail = the reason).
    """

    def __init__(self, runner: str, req: RunRequest, driver: Driver) -> None:
        self.runner = runner
        self.request = req
        self._driver = driver
        self._queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._ctx = RunContext(self)
        self._task: asyncio.Task[None] | None = None
        self._started_sent = False
        self._final: RunResult | None = None
        self._finished = False

    # -- iteration ---------------------------------------------------------

    def __aiter__(self) -> RunHandle:
        return self

    async def __anext__(self) -> RunEvent:
        if self._finished:
            raise StopAsyncIteration
        self._ensure_task()
        try:
            event = await self._queue.get()
        except asyncio.CancelledError:
            await self._stop_producer()
            raise
        if isinstance(event, Final):
            self._finished = True
            self._final = event.result
        return event

    async def __aenter__(self) -> RunHandle:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -- control -------------------------------------------------------------

    def cancel(self, reason: str = "cancelled") -> None:
        """Ask the run to stop. The process group is stopped and the iterator
        ends with a Final describing the cancellation."""
        if self._ctx.cancel_reason is None:
            self._ctx.cancel_reason = reason
        self._ctx.cancel_event.set()

    async def aclose(self) -> None:
        """Stop the run if it is still going and discard remaining events."""
        if self._task is not None and not self._task.done():
            await self._stop_producer()
        self._finished = True

    async def result(self) -> RunResult:
        """Consume all remaining events and return the final result."""
        async for _ in self:
            pass
        assert self._final is not None
        return self._final

    @property
    def done(self) -> bool:
        return self._finished

    # -- internals -------------------------------------------------------------

    def _ensure_task(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._produce(), name=f"runner:{self.runner}")
            _RUNNING.add(self._task)
            self._task.add_done_callback(_RUNNING.discard)

    async def _produce(self) -> None:
        start = time.monotonic()
        try:
            result = await self._driver(self._ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a runner bug must not escape as an exception
            log.exception("runner %s failed internally", self.runner)
            result = RunResult(
                ok=False,
                text="",
                session_id=self._ctx.session_id,
                error_kind="other",
                error_detail=f"internal runner error: {exc!r}",
                cost_usd=None,
                usage=None,
                duration_s=0.0,
                runner=self.runner,
                model=None,
            )
        result = replace(result, duration_s=round(time.monotonic() - start, 3), runner=self.runner)
        self._queue.put_nowait(Final(result))

    async def _stop_producer(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        # Shielded so the process group is stopped even if we get cancelled again.
        await asyncio.shield(asyncio.wait({task}))
