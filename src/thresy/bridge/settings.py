"""Settings of the bridge (``COACH_BRIDGE_*``), on top of the core settings in thresy.config."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_hhmm(value: str) -> time:
    match = _HHMM.match(value)
    if not match:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"not a time of day: {value!r}")
    return time(hour, minute)


@dataclass(frozen=True)
class QuietHours:
    """A local time window, possibly across midnight (``22:00-06:00``)."""

    start: time
    end: time

    @classmethod
    def parse(cls, value: str | None) -> QuietHours | None:
        if value is None or value.strip().lower() in ("", "off", "none", "no"):
            return None
        parts = value.split("-")
        if len(parts) != 2:
            raise ValueError(f"quiet hours must look like 22:00-06:00, got {value!r}")
        start, end = parse_hhmm(parts[0]), parse_hhmm(parts[1])
        if start == end:
            return None
        return cls(start, end)

    def contains(self, moment: time) -> bool:
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def end_after(self, local: datetime) -> datetime:
        """The next end of the window at or after ``local`` (aware, local time zone)."""
        tz = local.tzinfo
        candidate = datetime.combine(local.date(), self.end, tzinfo=tz)
        if candidate <= local:
            candidate = datetime.combine(local.date() + timedelta(days=1), self.end, tzinfo=tz)
        return candidate


ModelTier = Literal["fast", "smart"]


class BridgeSettings(BaseSettings):
    """Each field maps to ``COACH_<FIELD_NAME_IN_UPPER_CASE>`` (``COACH_BRIDGE_QUIET_HOURS``)."""

    model_config = SETTINGS_CONFIG

    bridge_transport: str = "telegram"
    """The chat transport; ``telegram`` is the only one so far (see docs/bridge.md for adding
    another)."""

    bridge_quiet_hours: str = "22:00-06:00"
    """Local window in which proactive messages (briefing, debrief, notifications) wait.
    ``off`` disables it. Answers to the athlete's own messages are never held back."""

    bridge_briefing: bool = True
    """Send a morning briefing (once per local day)."""
    bridge_briefing_fallback: str = "10:00"
    """If last night's sleep has not arrived by this local time, brief anyway and say so."""
    bridge_briefing_latest: str = "14:00"
    """No morning briefing is started after this local time."""
    bridge_night_end_earliest: str = "05:00"
    """Earliest local time a night can count as finished. If the stored sleep for today ends
    before this (a watch sync after waking up in the night), the briefing waits until the
    stored end moves later or until the fallback time, whichever comes first."""
    bridge_briefing_settle_s: float = 180
    """Delay between a finished-looking night and the briefing run, so that the sync pass that
    stored the sleep has also stored HRV and the daily stats."""

    bridge_debrief: bool = True
    """Send a debrief after a run is uploaded."""
    bridge_debrief_delay_s: float = 180
    """Wait this long after an upload so that uploads in a row become one debrief."""
    bridge_debrief_max_wait_s: float = 900
    """...but never longer than this after the first upload."""
    bridge_debrief_max_age_h: float = 12
    """Runs that started longer ago than this get no debrief (e.g. after a backfill)."""

    bridge_notify_interval_h: float = 12
    """At most one notification per kind (Garmin login, sync failures) in this many hours."""
    bridge_sync_failed_threshold: int = 1
    """Notify about sync failures only after this many ``sync_failed`` events within
    ``bridge_sync_failed_window_h``. The sync raises one only after several failed passes in a
    row (``COACH_SYNC_FAIL_EVENT_AFTER``), so 1 already ignores one-off blips."""
    bridge_sync_failed_window_h: float = 6

    bridge_run_timeout_s: float = 900
    bridge_chat_tier: ModelTier = "smart"
    bridge_proactive_tier: ModelTier = "smart"

    bridge_context_messages: int = 20
    """When a fresh session starts: at most this many earlier messages from the log..."""
    bridge_context_chars: int = 6000
    """...at most this many characters in total..."""
    bridge_context_hours: float = 36
    """...and none older than this."""

    bridge_requeue_minutes: float = 60
    """After a restart, messages that never reached a run and are younger than this are
    answered; older ones are left alone."""

    bridge_outbox_poll_s: float = 2
    bridge_events_poll_s: float = 15
    bridge_send_attempts: int = 5
    """Transient delivery failures of one outbox row before it is marked failed."""
    bridge_sync_request_timeout_s: float = 300
    """How long ``/sync`` waits for the sync service."""
    bridge_shutdown_grace_s: float = 20
    """On SIGTERM: how long to wait for a cancelled run to wind down and the outbox to flush."""

    @field_validator(
        "bridge_briefing_fallback", "bridge_briefing_latest", "bridge_night_end_earliest"
    )
    @classmethod
    def _check_time(cls, value: str) -> str:
        parse_hhmm(value)
        return value

    @field_validator("bridge_quiet_hours")
    @classmethod
    def _check_quiet(cls, value: str) -> str:
        QuietHours.parse(value)
        return value

    @property
    def quiet_hours(self) -> QuietHours | None:
        return QuietHours.parse(self.bridge_quiet_hours)

    @property
    def briefing_fallback(self) -> time:
        return parse_hhmm(self.bridge_briefing_fallback)

    @property
    def briefing_latest(self) -> time:
        return parse_hhmm(self.bridge_briefing_latest)

    @property
    def night_end_earliest(self) -> time:
        return parse_hhmm(self.bridge_night_end_earliest)


def local_day(moment: datetime, zone: ZoneInfo) -> date:
    return moment.astimezone(zone).date()
