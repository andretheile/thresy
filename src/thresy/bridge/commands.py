"""Chat commands the bridge answers itself, without an agent run.

``/status`` (sync, agent health, today's runs and cost, outbox), ``/sync`` (asks the sync
service through ``agent_requests`` and reports the result), ``/new`` (fresh session) and
``/help`` (``/start``, which Telegram sends on first contact, is the same). Other words
starting with ``/`` go to the agent like any message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg

from thresy.config import Settings
from thresy.runner import AgentRunner

from .agent import AgentService, BusyTracker
from .clock import Clock
from .repo import Repo
from .settings import BridgeSettings
from .texts import t

log = logging.getLogger(__name__)

COMMANDS = ("/status", "/sync", "/new", "/help", "/start")
FINISHED = ("done", "failed")


def parse_command(text: str) -> str | None:
    """``/Status@SomeBot extra`` -> ``/status``; None if the text is not a known command."""
    if not text or not text.startswith("/"):
        return None
    word = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return word if word in COMMANDS else None


class Commands:
    def __init__(
        self,
        *,
        repo: Repo,
        runner: AgentRunner,
        service: AgentService,
        settings: Settings,
        bridge_settings: BridgeSettings,
        clock: Clock,
        busy: BusyTracker,
        reply: Callable[[str, str], Awaitable[None]],
        is_working: Callable[[str], bool],
    ) -> None:
        self.repo = repo
        self.runner = runner
        self.service = service
        self.settings = settings
        self.bs = bridge_settings
        self.clock = clock
        self.busy = busy
        self.reply = reply
        self.is_working = is_working
        self.sync_poll_s = 2.0

    @property
    def lang(self) -> str:
        return self.settings.language

    async def handle(self, command: str, chat_id: str) -> None:
        try:
            if command in ("/help", "/start"):
                await self.reply(chat_id, t(self.lang, "help"))
            elif command == "/new":
                await self.service.reset_session(chat_id)
                await self.reply(chat_id, t(self.lang, "new"))
            elif command == "/status":
                await self.reply(chat_id, await self.status_text(chat_id))
            elif command == "/sync":
                await self.sync(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("command %s failed", command)
            await self.reply(chat_id, t(self.lang, "failed"))

    # -- /status ---------------------------------------------------------------------------------

    def _when(self, moment: datetime | None) -> str:
        if moment is None:
            return t(self.lang, "unknown")
        zone = self.service.prompt_env.zone
        local = moment.astimezone(zone)
        minutes = max(0, int((self.clock.now() - moment).total_seconds() // 60))
        if minutes < 60:
            ago = f"{minutes} min"
        elif minutes < 48 * 60:
            ago = f"{minutes // 60} h"
        else:
            ago = f"{minutes // (24 * 60)} d"
        return f"{local:%Y-%m-%d %H:%M} ({ago})"

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else None
        return None

    def _sync_lines(self, sync: dict[str, Any]) -> list[str]:
        lang = self.lang
        status = sync.get("sync_status") if isinstance(sync.get("sync_status"), dict) else {}
        auth = sync.get("garmin_auth") if isinstance(sync.get("garmin_auth"), dict) else {}
        lines = []
        last_success = self._parse_time(status.get("last_success_at"))
        if last_success is not None:
            lines.append(t(lang, "status_sync", value=self._when(last_success)))
        else:
            lines.append(
                t(lang, "status_sync_activity", value=self._when(sync["last_state_change"]))
            )
        lines.append(
            t(
                lang,
                "status_data",
                activity=sync["newest_activity"] or t(lang, "none"),
                sleep=sync["newest_sleep"] or t(lang, "none"),
            )
        )
        failures = int(status.get("consecutive_failures") or 0)
        if failures:
            error = " ".join(str(status.get("last_error") or "").split())[:200]
            value = f"{failures}× – {error}" if error else f"{failures}×"
            lines.append(t(lang, "status_sync_problem", value=value))
        if auth.get("status") and auth.get("status") != "ok":
            lines.append(t(lang, "status_garmin_login", value=auth["status"]))
        return lines

    async def status_text(self, chat_id: str) -> str:
        lang = self.lang
        zone = self.service.prompt_env.zone
        now = self.clock.now()
        local = now.astimezone(zone)
        midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=zone)
        lines = [f"**{t(lang, 'status_title')}**"]

        lines += self._sync_lines(await self.repo.sync_status())

        try:
            health = await asyncio.wait_for(self.runner.healthcheck(), timeout=30)
            value = f"{t(lang, 'ok') if health.ok else t(lang, 'not_ok')} – {health.detail}"
        except Exception as exc:
            value = f"{t(lang, 'not_ok')} – {type(exc).__name__}"
        lines.append(t(lang, "status_runner", runner=self.runner.name, value=value))

        runs = await self.repo.run_stats(midnight)
        cost = f"${runs['cost']:.2f}" if runs["cost"] is not None else "–"
        lines.append(t(lang, "status_runs", count=runs["count"], cost=cost))
        outbox = await self.repo.outbox_stats(midnight)
        lines.append(t(lang, "status_outbox", pending=outbox["pending"], failed=outbox["failed"]))
        if self.is_working(chat_id):
            lines.append(t(lang, "status_busy"))
        quiet = self.bs.quiet_hours
        if quiet is not None and quiet.contains(local.time().replace(tzinfo=None)):
            lines.append(t(lang, "status_quiet", until=f"{quiet.end_after(local):%H:%M}"))
        return "\n".join(lines)

    # -- /sync -----------------------------------------------------------------------------------

    async def sync(self, chat_id: str) -> None:
        lang = self.lang
        try:
            request_id = await self.repo.insert_request("sync_now", {})
        except psycopg.errors.UndefinedTable:
            await self.reply(chat_id, t(lang, "sync_unavailable"))
            return
        deadline = self.clock.now() + timedelta(seconds=self.bs.bridge_sync_request_timeout_s)
        await self.busy.acquire(chat_id)
        try:
            while True:
                row = await self.repo.request(request_id)
                if row is not None and row["status"] in FINISHED:
                    break
                if self.clock.now() >= deadline:
                    minutes = max(1, round(self.bs.bridge_sync_request_timeout_s / 60))
                    await self.reply(chat_id, t(lang, "sync_timeout", minutes=minutes))
                    return
                await asyncio.sleep(self.sync_poll_s)
        finally:
            await self.busy.release(chat_id)
        if row["status"] == "failed":
            error = " ".join(str(row.get("error") or t(lang, "unknown")).split())[:300]
            await self.reply(chat_id, t(lang, "sync_failed_cmd", error=error))
            return
        result = row.get("result") or {}
        new = result.get("new_activities") or []
        n = len(new) if isinstance(new, list) else int(new or 0)
        if n == 0:
            activities = t(lang, "sync_activities_0")
        elif n == 1:
            activities = t(lang, "sync_activities_1")
        else:
            activities = t(lang, "sync_activities_n", n=n)
        sleep = t(lang, "sync_sleep_yes" if result.get("sleep_synced") else "sync_sleep_no")
        await self.reply(chat_id, t(lang, "sync_done", activities=activities, sleep=sleep))
