"""A scriptable in-process runner for tests of other packages.

Echo mode (the default): Started, one Progress("text", prompt), then a
successful Final whose text is the prompt.

Scripted mode: pass a list (or a function of the request returning a list) of
Started / Progress / Final events and Delay / Fail markers::

    FakeRunner([Progress("tool", "Bash: ls"), Delay(0.1), Progress("text", "Done")])
    FakeRunner(fail="rate_limit")                      # Started, then a failed Final
    FakeRunner([Delay(60)])                            # hangs until the request's timeout
    FakeRunner(lambda req: [Progress("text", req.prompt.upper())])

Timeouts and RunHandle.cancel() behave like the real runners. Every request is
recorded in `runner.requests`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace

from .core import (
    ErrorKind,
    Final,
    Health,
    Progress,
    RunnerCapabilities,
    RunRequest,
    RunResult,
    Started,
)
from .handle import RunContext, RunHandle


@dataclass(frozen=True)
class Delay:
    """Pause the scripted run (counts against RunRequest.timeout_s)."""

    seconds: float


@dataclass(frozen=True)
class Fail:
    """End the scripted run with a failed Final of this kind."""

    kind: ErrorKind
    detail: str = "scripted failure"


ScriptItem = Started | Progress | Final | Delay | Fail
Script = Sequence[ScriptItem] | Callable[[RunRequest], Iterable[ScriptItem]]


class FakeRunner:
    name = "fake"
    capabilities = RunnerCapabilities(
        supports_resume=True,
        supports_streaming=True,
        supports_system_append=True,
        supports_effort=True,  # recorded in `requests`, otherwise without effect
    )

    def __init__(
        self,
        script: Script | None = None,
        *,
        delay_s: float = 0.0,
        fail: ErrorKind | None = None,
        fail_detail: str = "scripted failure",
        reply: str | None = None,
        session_id: str | None = None,
        model: str | None = "fake-model",
        cost_usd: float | None = None,
        usage: dict | None = None,
        health: Health | None = None,
    ) -> None:
        self.script = script
        self.delay_s = delay_s
        self.fail = fail
        self.fail_detail = fail_detail
        self.reply = reply
        self.session_id = session_id
        self.model = model
        self.cost_usd = cost_usd
        self.usage = usage
        self.requests: list[RunRequest] = []
        self._health = health or Health(
            runner=self.name,
            binary_found=True,
            binary_path=None,
            version="fake",
            auth_ok=True,
            detail="fake runner (no CLI)",
            verified_adapter=True,
        )

    def run(self, req: RunRequest) -> RunHandle:
        self.requests.append(req)
        return RunHandle(self.name, req, lambda ctx: self._drive(req, ctx))

    async def healthcheck(self) -> Health:
        return self._health

    def _items(self, req: RunRequest) -> list[ScriptItem]:
        if self.script is None:
            items: list[ScriptItem] = []
            if self.delay_s:
                items.append(Delay(self.delay_s))
            if self.fail is None:
                items.append(Progress("text", self.reply if self.reply is not None else req.prompt))
            return items
        if callable(self.script):
            return list(self.script(req))
        return list(self.script)

    async def _drive(self, req: RunRequest, ctx: RunContext) -> RunResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + req.timeout_s
        session = req.session_id or self.session_id or f"fake-{uuid.uuid4()}"
        items = self._items(req)
        last_text = ""

        def result(ok: bool, kind: ErrorKind | None = None, detail: str | None = None) -> RunResult:
            return RunResult(
                ok=ok,
                text=last_text,
                session_id=ctx.session_id or session,
                error_kind=None if ok else kind,
                error_detail=None if ok else detail,
                cost_usd=self.cost_usd,
                usage=self.usage,
                duration_s=0.0,
                runner=self.name,
                model=self.model,
            )

        def cancelled() -> RunResult:
            return result(False, "cancelled", ctx.cancel_reason or "cancelled")

        if not any(isinstance(item, Started) for item in items):
            ctx.emit(Started(session))
        for item in items:
            if ctx.cancel_event.is_set():
                return cancelled()
            if isinstance(item, Delay):
                remaining = deadline - loop.time()
                try:
                    await asyncio.wait_for(
                        ctx.cancel_event.wait(), min(item.seconds, max(remaining, 0))
                    )
                    return cancelled()
                except TimeoutError:
                    pass
                if item.seconds >= remaining:
                    return result(False, "timeout", f"no result within {req.timeout_s:g}s")
            elif isinstance(item, Fail):
                return result(False, item.kind, item.detail)
            elif isinstance(item, Final):
                return replace(item.result, session_id=item.result.session_id or session)
            elif isinstance(item, Started):
                ctx.emit(item)
            elif isinstance(item, Progress):
                ctx.emit(item)
                if item.kind == "text":
                    last_text = item.text
        if self.fail is not None:
            return result(False, self.fail, self.fail_detail)
        if self.script is None and self.reply is None:
            last_text = req.prompt
        return result(True)
