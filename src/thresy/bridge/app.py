"""The bridge: chat loop, chat commands, outbox delivery and event dispatcher in one process.

    bridge = Bridge(settings=..., bridge_settings=..., repo=repo, runner=runner,
                    transport=transport)
    await bridge.start()          # recovers from a previous run, starts receiving
    ...
    await bridge.shutdown()       # cancels the running agent, flushes the outbox

Flow of a chat message: the transport calls :meth:`Bridge.on_message`; the message is logged
in ``messages``; commands are answered directly; anything else goes to the chat's worker,
which runs the agent (one run at a time per chat, later messages batched). The agent's final
answer is queued in ``outbox`` and the deliverer sends it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from thresy.config import Settings
from thresy.runner import AgentRunner
from thresy.transport.base import InboundMessage, Transport

from . import prompts
from .agent import AgentService, BusyTracker
from .chat import ChatWorker, Job, Pending
from .clock import Clock, SystemClock
from .commands import Commands, parse_command
from .events import EventDispatcher
from .outbox import OutboxDeliverer
from .repo import Repo, Row
from .settings import BridgeSettings
from .texts import t

log = logging.getLogger(__name__)


class Bridge:
    def __init__(
        self,
        *,
        settings: Settings,
        bridge_settings: BridgeSettings,
        repo: Repo,
        runner: AgentRunner,
        transport: Transport,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.bs = bridge_settings
        self.repo = repo
        self.runner = runner
        self.transport = transport
        self.clock = clock or SystemClock()
        self.workspace = Path(settings.workspace_dir).expanduser().resolve()
        self.prompt_env = prompts.PromptEnv(
            workspace=self.workspace,
            zone=ZoneInfo(settings.tz),
            language=settings.language,
            transport=transport.name,
        )
        self.busy = BusyTracker(transport)
        self.outbox = OutboxDeliverer(
            repo=repo,
            transport=transport,
            workspace=self.workspace,
            clock=self.clock,
            bridge_settings=bridge_settings,
        )
        self.service = AgentService(
            repo=repo,
            runner=runner,
            settings=settings,
            bridge_settings=bridge_settings,
            clock=self.clock,
            busy=self.busy,
            prompt_env=self.prompt_env,
            wake_outbox=self.outbox.wake,
        )
        self.commands = Commands(
            repo=repo,
            runner=runner,
            service=self.service,
            settings=settings,
            bridge_settings=bridge_settings,
            clock=self.clock,
            busy=self.busy,
            reply=self.reply,
            is_working=self.is_working,
        )
        self.events = EventDispatcher(
            repo=repo,
            settings=settings,
            bridge_settings=bridge_settings,
            clock=self.clock,
            prompt_env=self.prompt_env,
            default_chat=lambda: transport.default_chat_id,
            enqueue=self.enqueue_job,
            notify=self.reply,
        )
        self.workers: dict[str, ChatWorker] = {}
        self.stopping = False
        self._tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self, *, background: bool = True) -> None:
        """Recover from the previous process, start receiving, and (unless ``background`` is
        False, as in tests) start the outbox and event loops."""
        for directory in (self.workspace / "out", self.workspace / "inbox"):
            directory.mkdir(parents=True, exist_ok=True)
        await self.recover()
        await self.transport.start(self.on_message)
        if background:
            self._loops = [
                asyncio.create_task(self.outbox.run_forever(), name="bridge-outbox"),
                asyncio.create_task(self.events.run_forever(), name="bridge-events"),
            ]
        self.outbox.wake()
        log.info(
            "bridge started: transport %s, runner %s, workspace %s",
            self.transport.name,
            self.runner.name,
            self.workspace,
        )

    async def recover(self) -> None:
        """Tell the athlete about runs a crash left unfinished; answer messages that never
        reached a run. Unsent outbox rows and unprocessed events are picked up by their loops."""
        interrupted = await self.repo.fail_running_runs("the bridge stopped during the run")
        for run in interrupted:
            proactive = run["trigger"] != "user_message"
            text = t(
                self.settings.language, "interrupted_proactive" if proactive else "interrupted"
            )
            if proactive:
                what = (
                    "what_morning_briefing"
                    if run["trigger"].startswith("morning_briefing")
                    else "what_post_run"
                    if run["trigger"].startswith("post_run")
                    else "what_proactive"
                )
                lang = self.settings.language
                text = t(lang, "proactive_prefix", what=t(lang, what)) + text
            log.warning("run %s was interrupted by a restart", run["id"])
            await self.repo.queue(chat_id=run["chat_id"], kind="text", text=text, run_id=run["id"])
        since = self.clock.now() - timedelta(minutes=self.bs.bridge_requeue_minutes)
        rows = await self.repo.unanswered_inbound(since)
        by_chat: dict[str, list[Pending]] = {}
        for row in rows:
            if not self.transport.allows(row["chat_id"]):
                continue
            by_chat.setdefault(row["chat_id"], []).append(await self._pending_from_row(row))
        for chat_id, items in by_chat.items():
            log.info("answering %d message(s) left over from before the restart", len(items))
            self.worker(chat_id).add_messages(items)

    async def shutdown(self) -> None:
        """Stop taking work, cancel running agents, flush the outbox, stop the transport."""
        if self.stopping:
            return
        self.stopping = True
        self.service.stopping = True
        log.info("bridge shutting down")
        for worker in self.workers.values():
            worker.stop()
        tasks = [w.task for w in self.workers.values() if not w.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.bs.bridge_shutdown_grace_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        for task in [*self._loops, *self._tasks]:
            task.cancel()
        for task in [*self._loops, *self._tasks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await asyncio.wait_for(self.outbox.deliver_pending(), timeout=10)
        except Exception:
            log.exception("could not flush the outbox; unsent rows go out after the restart")
        await self.transport.stop()
        log.info("bridge stopped")

    async def settle(self) -> None:
        """Wait until all chats are idle and command tasks are done, then deliver (tests)."""
        while True:
            for worker in list(self.workers.values()):
                await worker.wait_idle()
            if self._tasks:
                await asyncio.wait(list(self._tasks))
            if all(not w.busy for w in self.workers.values()) and not self._tasks:
                break
        await self.outbox.deliver_pending()

    # -- chat ------------------------------------------------------------------------------------

    def worker(self, chat_id: str) -> ChatWorker:
        worker = self.workers.get(chat_id)
        if worker is None:
            worker = ChatWorker(
                chat_id, service=self.service, bridge_settings=self.bs, clock=self.clock
            )
            self.workers[chat_id] = worker
        return worker

    def is_working(self, chat_id: str) -> bool:
        worker = self.workers.get(chat_id)
        return worker is not None and worker.running

    def enqueue_job(self, chat_id: str, job: Job) -> None:
        if not self.stopping:
            self.worker(chat_id).add_job(job)

    async def reply(self, chat_id: str, text: str) -> None:
        """A message from the bridge itself (goes through the outbox like everything else)."""
        await self.repo.queue(chat_id=chat_id, kind="text", text=text)
        self.outbox.wake()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_message(self, msg: InboundMessage) -> None:
        if not self.transport.allows(msg.chat_id):  # transports drop these already
            log.warning("dropped a message from chat %s: not allow-listed", msg.chat_id)
            return
        chat_id = msg.chat_id
        command = parse_command(msg.text) if msg.kind == "text" else None
        if command is not None:
            await self.repo.log_message(
                chat_id=chat_id,
                direction="in",
                kind="command",
                text=msg.text,
                transport=msg.transport,
                transport_msg_id=msg.message_id,
                ts=msg.ts,
            )
            if not self.stopping:
                self._spawn(self.commands.handle(command, chat_id))
            return

        text = msg.text
        question = None
        attachments: object = [a.as_json(self.workspace) for a in msg.attachments] or None
        if msg.kind == "choice_answer":
            ref = msg.reply_to
            if ref and await self.repo.choice_answered(chat_id, ref):
                log.info("ignored a second answer to choice %s", ref)
                return
            question, label = await self._choice(ref, msg.choice_index)
            text = text or label or ""
            attachments = {"reply_to": ref, "choice_index": msg.choice_index}
        message_id = await self.repo.log_message(
            chat_id=chat_id,
            direction="in",
            kind=msg.kind,
            text=text,
            attachments=attachments,
            transport=msg.transport,
            transport_msg_id=msg.message_id,
            ts=msg.ts,
        )
        if self.stopping:
            return  # logged; answered after the restart
        prompt_message = prompts.PromptMessage(
            ts=msg.ts or self.clock.now(),
            kind=msg.kind,
            text=text,
            attachments=attachments if isinstance(attachments, list) else (),
            question=question,
        )
        self.worker(chat_id).add_messages([Pending(message_id, prompt_message)])

    async def _choice(self, ref: str | None, index: int | None) -> tuple[str | None, str | None]:
        """The question text and the label of option ``index`` of outbox row ``ref``."""
        if not ref or not ref.isdigit():
            return None, None
        row = await self.repo.outbox_row(int(ref))
        if row is None:
            return None, None
        options = row.get("options") or []
        label = None
        if isinstance(options, list) and index is not None and 0 <= index < len(options):
            label = str(options[index])
        return row.get("text"), label

    async def _pending_from_row(self, row: Row) -> Pending:
        attachments = row.get("attachments")
        question = None
        if row["kind"] == "choice_answer" and isinstance(attachments, dict):
            question, _ = await self._choice(attachments.get("reply_to"), None)
        return Pending(
            row["id"],
            prompts.PromptMessage(
                ts=row["ts"],
                kind=row["kind"],
                text=row.get("text") or "",
                attachments=attachments if isinstance(attachments, list) else (),
                question=question,
            ),
        )
