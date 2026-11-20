"""Running the agent: sessions, the run record, the agent's environment, failures.

Conversation continuity belongs to the bridge. One session per chat and local day lives in
``agent_sessions``; the harness's own session id is resumed when the runner supports it and
the session was started with the same runner. Otherwise (new day, other runner, ``/new``, a
failed resume) a fresh harness session starts and the prompt carries recent messages from the
``messages`` log instead.

Every run is recorded in ``agent_runs``. Its final answer (or a plain message explaining why
there is none) is queued in the ``outbox``, after anything the agent queued itself with
``coach send``, and the outbox deliverer sends both in order.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from thresy.config import Settings
from thresy.runner import (
    AgentRunner,
    Effort,
    Final,
    RunHandle,
    RunRequest,
    RunResult,
    Started,
)
from thresy.transport.base import Transport

from . import prompts
from .briefing import numbers_briefing
from .clock import Clock
from .credential import CredentialWatch
from .repo import Repo
from .settings import BridgeSettings, ModelTier
from .texts import t

log = logging.getLogger(__name__)

RETRY_FRESH_KINDS = ("other", "crash")


class BusyTracker:
    """Reference-counted busy indicator per chat (agent runs and /sync can overlap)."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.counts: dict[str, int] = defaultdict(int)

    async def _set(self, chat_id: str, busy: bool) -> None:
        try:
            await self.transport.set_busy(chat_id, busy)
        except Exception:
            log.exception("busy indicator failed")

    async def acquire(self, chat_id: str) -> None:
        self.counts[chat_id] += 1
        if self.counts[chat_id] == 1:
            await self._set(chat_id, True)

    async def release(self, chat_id: str) -> None:
        if self.counts[chat_id] <= 0:
            return
        self.counts[chat_id] -= 1
        if self.counts[chat_id] == 0:
            await self._set(chat_id, False)

    def is_busy(self, chat_id: str) -> bool:
        return self.counts.get(chat_id, 0) > 0


@dataclass
class RunSpec:
    """One agent run the chat worker asked for."""

    chat_id: str
    trigger: str
    """``user_message``, ``morning_briefing``, ``post_run`` or several joined by ``+``."""
    tier: ModelTier
    proactive: bool
    build_prompt: Callable[[str | None], str]
    """Builds the prompt; the argument is the recent-conversation block for fresh sessions."""
    message_ids: list[int] = field(default_factory=list)
    """``messages`` rows this run answers (excluded from the context block)."""
    what: str = "what_proactive"
    """Text key naming a proactive run in failure messages."""
    numbers_day: date | None = None
    """Send the numbers-only briefing for this day if the run fails."""
    on_start: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_done: list[Callable[[RunResult | None], Awaitable[None]]] = field(default_factory=list)
    effort: Effort | None = None
    """Reasoning effort for the runner; None: the harness's default."""
    collect: Callable[[], Awaitable[None]] | None = None
    """Called right before the prompt is built: adds the messages that are waiting for this
    chat by then to this run (``message_ids`` and the prompt)."""
    waiting: Callable[[], Awaitable[set[int]]] | None = None
    """The ``messages`` rows received for this chat that wait for a later run; they are left
    out of the context block (the agent sees them once, as messages to answer)."""


@dataclass(frozen=True)
class Outcome:
    result: RunResult
    run_id: int
    retried: bool


def _failed(detail: str) -> RunResult:
    return RunResult(
        ok=False,
        text="",
        session_id=None,
        error_kind="other",
        error_detail=detail,
        cost_usd=None,
        usage=None,
        duration_s=0.0,
        runner="?",
        model=None,
    )


def is_no_reply(text: str) -> bool:
    return text.strip().strip(".").strip().upper() == prompts.NO_REPLY


class AgentService:
    def __init__(
        self,
        *,
        repo: Repo,
        runner: AgentRunner,
        settings: Settings,
        bridge_settings: BridgeSettings,
        clock: Clock,
        busy: BusyTracker,
        prompt_env: prompts.PromptEnv,
        wake_outbox: Callable[[], None],
        credential: CredentialWatch | None = None,
    ) -> None:
        self.repo = repo
        self.runner = runner
        self.settings = settings
        self.bs = bridge_settings
        self.clock = clock
        self.busy = busy
        self.prompt_env = prompt_env
        self.wake_outbox = wake_outbox
        self.credential = credential
        """Told what each run reports about the credential's end."""
        self.handles: dict[str, RunHandle] = {}
        self.stopping = False

    # -- environment -----------------------------------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self.prompt_env.workspace

    def agent_env(self, run_id: int) -> dict[str, str]:
        """Exactly what the agent's ``coach`` commands need, on top of the runner's allow-list.

        Never the Telegram token, never the read/write database URL: the agent gets the
        ``coach_agent`` URL as its ``COACH_DATABASE_URL`` and the ``coach_ro`` URL.
        """
        s = self.settings
        env = {
            "COACH_DATABASE_URL": s.require_agent_database_url(),
            "COACH_DATA_DIR": str(s.data_dir.expanduser().resolve()),
            "COACH_WORKSPACE_DIR": str(self.workspace),
            "COACH_TZ": s.tz,
            "TZ": s.tz,
            "COACH_LANGUAGE": s.language,
            "COACH_NAME": s.name,
            "COACH_RUN_ID": str(run_id),
        }
        if s.database_url_ro:
            env["COACH_DATABASE_URL_RO"] = s.database_url_ro
        if self.bs.home_location and self.bs.home_location.strip():
            env["COACH_HOME_LOCATION"] = self.bs.home_location.strip()
        return env

    # -- sessions --------------------------------------------------------------------------------

    def today(self) -> date:
        return self.clock.now().astimezone(self.prompt_env.zone).date()

    async def session_for(self, chat_id: str) -> dict[str, Any]:
        session = await self.repo.active_session(chat_id)
        if session is not None:
            if session["day"] == self.today() and session["runner"] == self.runner.name:
                return session
            log.info(
                "new session for chat %s (%s)",
                chat_id,
                "runner changed" if session["runner"] != self.runner.name else "new day",
            )
        return await self.repo.create_session(chat_id, self.runner.name, self.today())

    async def reset_session(self, chat_id: str) -> None:
        """``/new``: close the active session and start the next one without earlier context.

        The cut is remembered by message id (``bridge:reset:<chat>`` in sync_state), so the
        new session's context block holds nothing from before the command.
        """
        session = await self.repo.active_session(chat_id)
        if session is not None:
            await self.repo.set_session_status(session["id"], "reset")
        new = await self.repo.create_session(chat_id, self.runner.name, self.today())
        await self.repo.set_state(
            f"reset:{chat_id}",
            {
                "session": str(new["id"]),
                "after_message_id": await self.repo.last_message_id(chat_id),
            },
        )

    async def context_for(self, chat_id: str, session: dict[str, Any], spec: RunSpec) -> str | None:
        since = self.clock.now() - timedelta(hours=self.bs.bridge_context_hours)
        after_id = 0
        reset = await self.repo.get_state(f"reset:{chat_id}")
        if isinstance(reset, dict) and reset.get("session") == str(session["id"]):
            after_id = int(reset.get("after_message_id") or 0)  # /new: nothing from before
        rows = await self.repo.recent_messages(
            chat_id, since, spec.message_ids, self.bs.bridge_context_messages, after_id
        )
        if spec.waiting is not None:
            # Asked after the query: a message logged before it is in the worker by then.
            waiting = await spec.waiting()
            rows = [row for row in rows if row["id"] not in waiting]
        return prompts.context_block(self.prompt_env, rows, self.bs.bridge_context_chars)

    # -- runs ------------------------------------------------------------------------------------

    def cancel(self, chat_id: str, reason: str) -> bool:
        handle = self.handles.get(chat_id)
        if handle is None:
            return False
        handle.cancel(reason)
        return True

    def cancel_all(self, reason: str) -> None:
        for handle in list(self.handles.values()):
            handle.cancel(reason)

    async def execute(self, spec: RunSpec) -> Outcome:
        for callback in spec.on_start:
            await callback()
        result: RunResult | None = None
        try:
            session = await self.session_for(spec.chat_id)
            resume = (
                session["native_session_id"] if self.runner.capabilities.supports_resume else None
            )
            result, run_id = await self._attempt(spec, session, resume)
            retried = False
            if (
                resume is not None
                and not result.ok
                and result.error_kind in RETRY_FRESH_KINDS
                and not self.stopping
                and await self.repo.outbox_rows_for_run(run_id) == 0
            ):
                log.warning(
                    "resumed run %s failed (%s); retrying once in a fresh session",
                    run_id,
                    result.error_kind,
                )
                await self.repo.set_session_status(session["id"], "failed")
                session = await self.repo.create_session(
                    spec.chat_id, self.runner.name, self.today()
                )
                result, run_id = await self._attempt(spec, session, None)
                retried = True
            await self._conclude(spec, result, run_id)
            return Outcome(result=result, run_id=run_id, retried=retried)
        finally:
            for done in spec.on_done:
                try:
                    await done(result)
                except Exception:
                    log.exception("run completion callback failed")

    async def _attempt(
        self, spec: RunSpec, session: dict[str, Any], resume: str | None
    ) -> tuple[RunResult, int]:
        if spec.collect is not None:
            await spec.collect()  # messages waiting by now go to this run, not the next one
        context = None if resume else await self.context_for(spec.chat_id, session, spec)
        prompt = spec.build_prompt(context)
        run_id = await self.repo.create_run(session["id"], spec.trigger, self.runner.name)
        await self.repo.attach_messages(spec.message_ids, run_id)
        request = RunRequest(
            prompt=prompt,
            cwd=self.workspace,
            session_id=resume,
            model_tier=spec.tier,
            timeout_s=self.bs.bridge_run_timeout_s,
            env=self.agent_env(run_id),
            effort=spec.effort,
        )
        log.info(
            "run %s (%s) for chat %s: %s session, tier %s, effort %s",
            run_id,
            spec.trigger,
            spec.chat_id,
            "resumed" if resume else "fresh",
            spec.tier,
            spec.effort or "default",
        )
        stored = resume
        result: RunResult | None = None
        await self.busy.acquire(spec.chat_id)
        try:
            handle = self.runner.run(request)
            self.handles[spec.chat_id] = handle
            if self.stopping:
                handle.cancel("shutdown")
            async with handle:
                async for event in handle:
                    new_id = event.session_id if isinstance(event, Started) else None
                    if new_id and new_id != stored:
                        stored = new_id
                        await self.repo.set_native_session(session["id"], stored)
                    elif isinstance(event, Final):
                        result = event.result
        except asyncio.CancelledError:
            await asyncio.shield(
                self.repo.finish_run(
                    run_id, status="failed", error_kind="cancelled", error_detail="task cancelled"
                )
            )
            raise
        except Exception as exc:  # a bug here must not leave the run 'running'
            log.exception("run %s failed in the bridge", run_id)
            result = _failed(f"bridge error: {exc!r}")
        finally:
            self.handles.pop(spec.chat_id, None)
            await self.busy.release(spec.chat_id)
        if result is None:
            result = _failed("the run ended without a result")
        if result.session_id and result.session_id != stored:
            await self.repo.set_native_session(session["id"], result.session_id)
        await self.repo.finish_run(
            run_id,
            status="ok" if result.ok else "failed",
            model=result.model,
            cost_usd=result.cost_usd,
            usage=result.usage,
            error_kind=result.error_kind,
            error_detail=result.error_detail,
        )
        await self.repo.touch_session(session["id"])
        if self.credential is not None:
            try:
                await self.credential.observe(result)
            except Exception:  # never at the cost of the answer
                log.exception("could not record the credential's end")
        log.info(
            "run %s finished: %s%s",
            run_id,
            "ok" if result.ok else "failed",
            f" ({result.error_kind}: {result.error_detail})" if not result.ok else "",
        )
        return result, run_id

    def failure_text(self, spec: RunSpec, result: RunResult) -> str:
        lang = self.settings.language
        kind = result.error_kind
        detail = prompts_one_line(result.error_detail or "", 200)
        if kind == "rate_limit":
            text = t(lang, "rate_limit")
            if detail:
                text += "\n" + t(lang, "detail", detail=detail)
        elif kind == "auth":
            text = t(lang, "auth")
            if detail:
                text += "\n" + t(lang, "detail", detail=detail)
        elif kind == "timeout":
            text = t(lang, "timeout")
        elif kind == "cancelled" and self.stopping:
            text = t(lang, "interrupted_proactive" if spec.proactive else "interrupted")
        else:
            text = t(lang, "failed")
        if spec.proactive:
            text = t(lang, "proactive_prefix", what=t(lang, spec.what)) + text
        return text

    async def _conclude(self, spec: RunSpec, result: RunResult, run_id: int) -> None:
        lang = self.settings.language
        if result.ok:
            text = result.text.strip()
            if text and not is_no_reply(text):
                await self.repo.queue(chat_id=spec.chat_id, kind="text", text=text, run_id=run_id)
            elif (
                not text and not spec.proactive and await self.repo.outbox_rows_for_run(run_id) == 0
            ):
                await self.repo.queue(
                    chat_id=spec.chat_id, kind="text", text=t(lang, "no_answer"), run_id=run_id
                )
        else:
            await self.repo.queue(
                chat_id=spec.chat_id,
                kind="text",
                text=self.failure_text(spec, result),
                run_id=run_id,
            )
            if spec.numbers_day is not None:
                try:
                    numbers = await numbers_briefing(self.repo, lang, spec.numbers_day)
                except Exception:
                    log.exception("numbers-only briefing failed")
                else:
                    await self.repo.queue(
                        chat_id=spec.chat_id, kind="text", text=numbers, run_id=run_id
                    )
        self.wake_outbox()


def prompts_one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
