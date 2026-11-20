"""One worker per chat: one agent run at a time, later messages wait and go together.

Messages that arrive while a run is in progress are collected and handed to the next run as
one batch (not dropped, not run concurrently). Proactive jobs (briefing, debrief) queue in the
same worker, after waiting messages; several due jobs are combined into one run so the athlete
gets one message instead of two in a row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date

from thresy.runner import RunResult

from . import prompts
from .agent import AgentService, RunSpec
from .clock import Clock
from .settings import BridgeSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pending:
    message_id: int
    """The ``messages`` row of this inbound message."""
    message: prompts.PromptMessage


@dataclass
class Job:
    """A proactive run requested by the event dispatcher."""

    build: Callable[[], Awaitable[prompts.ProactiveSection]]
    """Builds the job's part of the prompt when the run starts, from the data as it is then
    (never from what was stored when the job was triggered)."""
    what: str
    """Text key naming the job in failure messages (``what_morning_briefing``...)."""
    numbers_day: date | None = None
    on_start: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_done: list[Callable[[RunResult | None], Awaitable[None]]] = field(default_factory=list)


class ChatWorker:
    def __init__(
        self,
        chat_id: str,
        *,
        service: AgentService,
        bridge_settings: BridgeSettings,
        clock: Clock,
    ) -> None:
        self.chat_id = chat_id
        self.service = service
        self.bs = bridge_settings
        self.clock = clock
        self.pending: list[Pending] = []
        self.jobs: list[Job] = []
        self.running = False
        self.stopping = False
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self.task: asyncio.Task[None] = asyncio.create_task(
            self._loop(), name=f"chat-worker:{chat_id}"
        )

    # -- queueing --------------------------------------------------------------------------------

    def add_messages(self, items: list[Pending]) -> None:
        if not items or self.stopping:
            return
        self.pending.extend(items)
        self._kick()

    def add_job(self, job: Job) -> None:
        if self.stopping:
            return
        self.jobs.append(job)
        self._kick()

    def _kick(self) -> None:
        self._idle.clear()
        self._wake.set()

    @property
    def busy(self) -> bool:
        return self.running or bool(self.pending) or bool(self.jobs)

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def stop(self) -> None:
        """Stop taking work and cancel the current run (it ends with a recorded failure)."""
        self.stopping = True
        self.service.cancel(self.chat_id, "shutdown")
        self._wake.set()

    # -- the loop --------------------------------------------------------------------------------

    async def _sections(self, jobs: list[Job]) -> tuple[list[Job], list[prompts.ProactiveSection]]:
        kept, sections = [], []
        for job in jobs:
            try:
                sections.append(await job.build())
            except Exception:
                log.exception("chat %s: could not prepare a %s; dropped", self.chat_id, job.what)
                for done in job.on_done:
                    try:
                        await done(None)
                    except Exception:
                        log.exception("job completion callback failed")
                continue
            kept.append(job)
        return kept, sections

    async def _next_spec(self) -> RunSpec | None:
        env = self.service.prompt_env
        if self.pending:
            batch, self.pending = self.pending, []
            messages = [p.message for p in batch]
            return RunSpec(
                chat_id=self.chat_id,
                trigger="user_message",
                tier=self.bs.bridge_chat_tier,
                effort=self.bs.bridge_chat_effort,
                proactive=False,
                build_prompt=lambda ctx: prompts.chat_prompt(env, messages, context=ctx),
                message_ids=[p.message_id for p in batch],
            )
        if self.jobs:
            jobs, self.jobs = self.jobs, []
            jobs, sections = await self._sections(jobs)
            if not jobs:
                return await self._next_spec()
            triggers = list(dict.fromkeys(s.trigger for s in sections))
            now = self.clock.now()
            return RunSpec(
                chat_id=self.chat_id,
                trigger="+".join(triggers),
                tier=self.bs.bridge_proactive_tier,
                effort=self.bs.bridge_proactive_effort,
                proactive=True,
                build_prompt=lambda ctx: prompts.proactive_prompt(env, sections, now, context=ctx),
                what=jobs[0].what if len(jobs) == 1 else "what_proactive",
                numbers_day=next((j.numbers_day for j in jobs if j.numbers_day), None),
                on_start=[cb for j in jobs for cb in j.on_start],
                on_done=[cb for j in jobs for cb in j.on_done],
            )
        return None

    async def _loop(self) -> None:
        while not self.stopping:
            await self._wake.wait()
            self._wake.clear()
            while not self.stopping:
                self.running = True
                try:
                    spec = await self._next_spec()
                    if spec is None:
                        break
                    await self.service.execute(spec)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("chat %s: run failed in the bridge", self.chat_id)
                finally:
                    self.running = False
            if not self.pending and not self.jobs:
                self._idle.set()
        self._idle.set()
