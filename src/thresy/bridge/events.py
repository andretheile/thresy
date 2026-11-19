"""Turning sync events into proactive messages.

* ``sleep_arrived`` for today's wake date starts the morning briefing, once per local day.
  If no sleep has arrived by ``COACH_BRIDGE_BRIEFING_FALLBACK``, the briefing goes out anyway
  and says the night is not synced yet. No briefing starts after
  ``COACH_BRIDGE_BRIEFING_LATEST``.
* ``new_activity`` for a run starts a post-run debrief after a short delay; uploads in a row
  are merged into one debrief.
* ``garmin_auth_required`` and ``sync_failed`` become plain notifications (no agent run),
  rate-limited per kind; sync failures only after a threshold.
* During quiet hours proactive messages wait until the window ends.

An event is marked processed when it is handled (or deliberately skipped, with the reason in
``processed_by``). Events that wait (quiet hours, debrief delay) stay unprocessed in the
database, so a restart picks them up again. The day of the last briefing and the time of the
last notification per kind are kept in ``sync_state`` (``bridge:`` keys), so a restart does
not repeat them.

Everything time-dependent goes through :meth:`EventDispatcher.tick`, which reads the injected
clock; the background loop only calls ``tick`` periodically.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from thresy.config import Settings
from thresy.runner import RunResult

from . import prompts
from .chat import Job
from .clock import Clock
from .repo import Repo, Row
from .settings import BridgeSettings
from .texts import t

log = logging.getLogger(__name__)

BRIEFING_STATE = "briefing"
NOTIFIED_STATE = "notified:{kind}"
PROCESSED_BY = "bridge:{outcome}"


@dataclass
class PendingBriefing:
    day: date
    synced: bool
    event_ids: list[int] = field(default_factory=list)
    enqueued: bool = False


@dataclass
class PendingDebrief:
    first_seen: datetime
    due: datetime
    event_ids: list[int] = field(default_factory=list)
    activity_ids: list[int] = field(default_factory=list)


class EventDispatcher:
    def __init__(
        self,
        *,
        repo: Repo,
        settings: Settings,
        bridge_settings: BridgeSettings,
        clock: Clock,
        prompt_env: prompts.PromptEnv,
        default_chat: Callable[[], str | None],
        enqueue: Callable[[str, Job], None],
        notify: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.bs = bridge_settings
        self.clock = clock
        self.env = prompt_env
        self.default_chat = default_chat
        self.enqueue = enqueue
        self.notify = notify
        self.claimed: set[int] = set()
        self.briefing: PendingBriefing | None = None
        self.debrief: PendingDebrief | None = None
        self.deferred: list[Row] = []

    # -- loop ------------------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event dispatcher tick failed")
            await asyncio.sleep(self.bs.bridge_events_poll_s)

    def _local(self) -> datetime:
        return self.clock.now().astimezone(self.env.zone)

    def _quiet(self, local: datetime) -> bool:
        quiet = self.bs.quiet_hours
        return quiet is not None and quiet.contains(local.time().replace(tzinfo=None))

    async def tick(self) -> None:
        now = self.clock.now()
        local = self._local()
        quiet = self._quiet(local)
        for event in await self.repo.unprocessed_events():
            if event["id"] in self.claimed:
                continue
            try:
                await self._handle(event, now, local, quiet)
            except Exception:
                log.exception("event %s (%s) could not be handled", event["id"], event["kind"])
        await self._maybe_fallback_briefing(local, quiet)
        if not quiet:
            await self._release(now, local)

    async def _mark(self, ids: list[int], outcome: str) -> None:
        await self.repo.mark_events(ids, PROCESSED_BY.format(outcome=outcome))
        self.claimed.difference_update(ids)

    async def _handle(self, event: Row, now: datetime, local: datetime, quiet: bool) -> None:
        kind = event["kind"]
        payload = event.get("payload") or {}
        if kind == "sleep_arrived":
            await self._on_sleep(event, payload, local)
        elif kind == "new_activity":
            await self._on_activity(event, payload, now)
        elif kind in ("garmin_auth_required", "sync_failed"):
            if quiet:
                self.claimed.add(event["id"])
                self.deferred.append(event)
            else:
                await self._notify_problem(event, now)
        else:
            await self._mark([event["id"]], "ignored")

    # -- morning briefing ------------------------------------------------------------------------

    async def _briefed(self, day: date) -> bool:
        state = await self.repo.get_state(BRIEFING_STATE)
        return isinstance(state, dict) and state.get("date") == day.isoformat()

    async def _on_sleep(self, event: Row, payload: dict[str, Any], local: datetime) -> None:
        today = local.date()
        if not self.bs.bridge_briefing:
            await self._mark([event["id"]], "briefing-off")
        elif str(payload.get("wake_date")) != today.isoformat():
            await self._mark([event["id"]], "not-today")
        elif await self._briefed(today):
            await self._mark([event["id"]], "already-briefed")
        elif local.time().replace(tzinfo=None) >= self.bs.briefing_latest:
            await self._mark([event["id"]], "too-late")
        elif self.briefing is not None and self.briefing.day == today:
            self.claimed.add(event["id"])
            self.briefing.event_ids.append(event["id"])
            self.briefing.synced = True
        else:
            self.claimed.add(event["id"])
            self.briefing = PendingBriefing(day=today, synced=True, event_ids=[event["id"]])

    async def _maybe_fallback_briefing(self, local: datetime, quiet: bool) -> None:
        if not self.bs.bridge_briefing or self.briefing is not None:
            return
        moment = local.time().replace(tzinfo=None)
        if not (self.bs.briefing_fallback <= moment < self.bs.briefing_latest):
            return
        today = local.date()
        if await self._briefed(today):
            return
        synced = await self.repo.sleep_synced(today)
        log.info("no briefing yet at %s; briefing now (sleep synced: %s)", moment, synced)
        self.briefing = PendingBriefing(day=today, synced=synced)

    def _briefing_job(self, pending: PendingBriefing) -> Job:
        def build() -> prompts.ProactiveSection:
            return prompts.briefing_section(
                self.env,
                pending.day,
                synced=pending.synced,
                fallback_time=self.bs.bridge_briefing_fallback,
            )

        async def started() -> None:
            await self.repo.set_state(
                BRIEFING_STATE,
                {"date": pending.day.isoformat(), "at": self.clock.now().isoformat()},
            )

        async def done(result: RunResult | None) -> None:
            await self._mark(pending.event_ids, "briefing")
            if self.briefing is pending:
                self.briefing = None

        return Job(
            build=build,
            what="what_morning_briefing",
            numbers_day=pending.day,
            on_start=[started],
            on_done=[done],
        )

    # -- debrief ---------------------------------------------------------------------------------

    async def _on_activity(self, event: Row, payload: dict[str, Any], now: datetime) -> None:
        if not payload.get("is_run"):
            await self._mark([event["id"]], "not-a-run")
            return
        if not self.bs.bridge_debrief:
            await self._mark([event["id"]], "debrief-off")
            return
        try:
            activity_id = int(payload["activity_id"])
        except (KeyError, TypeError, ValueError):
            await self._mark([event["id"]], "invalid")
            return
        activity = await self.repo.activity(activity_id)
        started = (activity or {}).get("start_utc") or event["ts"]
        if now - started > timedelta(hours=self.bs.bridge_debrief_max_age_h):
            await self._mark([event["id"]], "too-old")
            return
        self.claimed.add(event["id"])
        delay = timedelta(seconds=self.bs.bridge_debrief_delay_s)
        if self.debrief is None:
            self.debrief = PendingDebrief(first_seen=now, due=now + delay)
        cap = self.debrief.first_seen + timedelta(seconds=self.bs.bridge_debrief_max_wait_s)
        self.debrief.due = min(now + delay, cap)
        self.debrief.event_ids.append(event["id"])
        if activity_id not in self.debrief.activity_ids:
            self.debrief.activity_ids.append(activity_id)

    async def _debrief_job(self, pending: PendingDebrief) -> Job:
        activities = []
        for activity_id in pending.activity_ids:
            row = await self.repo.activity(activity_id)
            activities.append(row or {"id": activity_id})
        section = prompts.debrief_section(self.env, activities)

        async def done(result: RunResult | None) -> None:
            await self._mark(pending.event_ids, "debrief")

        return Job(build=lambda: section, what="what_post_run", on_done=[done])

    # -- release ---------------------------------------------------------------------------------

    async def _release(self, now: datetime, local: datetime) -> None:
        chat = self.default_chat()
        if chat is None:
            return
        briefing = self.briefing
        if briefing is not None and not briefing.enqueued:
            if briefing.day != local.date():
                await self._mark(briefing.event_ids, "expired")
                self.briefing = None
            else:
                briefing.enqueued = True
                self.enqueue(chat, self._briefing_job(briefing))
        debrief = self.debrief
        if debrief is not None and now >= debrief.due:
            self.debrief = None
            self.enqueue(chat, await self._debrief_job(debrief))
        deferred, self.deferred = self.deferred, []
        for event in deferred:
            await self._notify_problem(event, now)

    # -- notifications ---------------------------------------------------------------------------

    async def _recently_notified(self, kind: str, now: datetime) -> bool:
        value = await self.repo.get_state(NOTIFIED_STATE.format(kind=kind))
        if not isinstance(value, str):
            return False
        try:
            last = datetime.fromisoformat(value)
        except ValueError:
            return False
        return now - last < timedelta(hours=self.bs.bridge_notify_interval_h)

    async def _notify_problem(self, event: Row, now: datetime) -> None:
        kind = event["kind"]
        payload = event.get("payload") or {}
        lang = self.settings.language
        chat = self.default_chat()
        if chat is None:
            await self._mark([event["id"]], "no-chat")
            return
        if await self._recently_notified(kind, now):
            await self._mark([event["id"]], "rate-limited")
            return
        if kind == "garmin_auth_required":
            text = t(lang, "garmin_auth")
            reason = payload.get("reason")
        else:
            if await self._recently_notified("garmin_auth_required", now):
                await self._mark([event["id"]], "covered-by-auth")
                return
            window = now - timedelta(hours=self.bs.bridge_sync_failed_window_h)
            stats = await self.repo.count_events("sync_failed", window)
            if int(stats["n"]) < self.bs.bridge_sync_failed_threshold:
                await self._mark([event["id"]], "below-threshold")
                return
            since = (stats["first"] or event["ts"]).astimezone(self.env.zone)
            text = t(lang, "sync_failed", count=int(stats["n"]), since=since.strftime("%H:%M"))
            reason = payload.get("error")
        if reason:
            text += "\n" + t(lang, "last_error", error=" ".join(str(reason).split())[:200])
        await self.notify(chat, text)
        await self.repo.set_state(NOTIFIED_STATE.format(kind=kind), now.isoformat())
        await self._mark([event["id"]], "notified")
