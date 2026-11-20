"""``coach backup schedule``: the backup service of the Compose stack.

* One backup at ``COACH_BACKUP_AT`` (local time, ``COACH_TZ``) on each of
  ``COACH_BACKUP_WEEKDAYS`` (default: every day).
* Right after start, when there is no successful backup since the last scheduled time (none
  at all, or the service was down or failed when it was due), one backup after
  :data:`START_DELAY_S`: a new deployment gets its first backup the same day, a server that
  was down catches up, and a restart between two scheduled times changes nothing.
* A failed run is retried after :data:`RETRY_AFTER_S`, at most :data:`MAX_RETRIES` times, then
  the next scheduled time is awaited.
* When the last restore check is ``COACH_BACKUP_RESTORE_CHECK_DAYS`` old (less
  :data:`CHECK_SLACK`), the run also restores its dump into a scratch database.
* SIGTERM or SIGINT: no new run starts; a running one stops at its next step.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from thresy import db

from .core import BackupConfig, BackupError, get_status, run_backup
from .settings import BackupSettings, describe_weekdays

log = logging.getLogger(__name__)

START_DELAY_S = 60.0
RETRY_AFTER_S = 3600.0
MAX_RETRIES = 2
CHECK_SLACK = timedelta(hours=12)
"""A restore check counts as due this much early, so that backups at the same local time N days
apart qualify although the last check finished a few minutes after its backup started (or a
change to or from daylight saving time made the gap an hour shorter)."""


def _scheduled(day: date, settings: BackupSettings, zone: ZoneInfo) -> datetime:
    # A time that does not exist that day (skipped by a change to daylight saving time) is
    # taken with the offset from before the change, so it runs an hour later on the clock.
    return datetime.combine(day, settings.at, tzinfo=zone).astimezone(UTC)


def next_time(now: datetime, settings: BackupSettings, zone: ZoneInfo) -> datetime:
    """The first scheduled time after ``now`` (aware datetimes)."""
    weekdays = settings.weekdays
    today = now.astimezone(zone).date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        moment = _scheduled(day, settings, zone)
        if moment > now:
            return moment
    raise ValueError("no weekday to schedule the backup on")  # the settings refuse that


def previous_time(now: datetime, settings: BackupSettings, zone: ZoneInfo) -> datetime:
    """The last scheduled time at or before ``now``."""
    weekdays = settings.weekdays
    today = now.astimezone(zone).date()
    for offset in range(8):
        day = today - timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        moment = _scheduled(day, settings, zone)
        if moment <= now:
            return moment
    raise ValueError("no weekday to schedule the backup on")


def describe(settings: BackupSettings, zone: ZoneInfo) -> str:
    """``daily at 03:30 (Europe/Berlin)``, ``sun at 03:30 (UTC)``."""
    return f"{describe_weekdays(settings.weekdays)} at {settings.at:%H:%M} ({zone.key})"


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def restore_check_due(status: dict, days: int, now: datetime) -> bool:
    if days <= 0:
        return False
    check = status.get("restore_check")
    last = _parse(check.get("at")) if isinstance(check, dict) and check.get("ok") else None
    return last is None or now - last >= timedelta(days=days) - CHECK_SLACK


def catch_up_due(status: dict, now: datetime, settings: BackupSettings, zone: ZoneInfo) -> bool:
    """No successful backup since the last scheduled time."""
    last = _parse(status.get("last_success_at"))
    return last is None or last < previous_time(now, settings, zone)


def run_forever(
    config: BackupConfig,
    settings: BackupSettings,
    zone: ZoneInfo,
    *,
    stop: threading.Event | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    stop = stop or threading.Event()
    log.info("backup schedule: %s", describe(settings, zone))
    with db.connect(config.database_url) as conn:
        status = get_status(conn)
    due = None
    if catch_up_due(status, now(), settings, zone):
        log.info("no successful backup since the last scheduled time; catching up")
        due = now() + timedelta(seconds=START_DELAY_S)
    retries = 0
    while not stop.is_set():
        if due is None:
            due = next_time(now(), settings, zone)
            retries = 0
        log.info("next backup at %s", due.astimezone(zone).isoformat(timespec="minutes"))
        while not stop.is_set() and now() < due:
            stop.wait(min(60.0, max(0.1, (due - now()).total_seconds())))
        if stop.is_set():
            break
        try:
            with db.connect(config.database_url) as conn:
                status = get_status(conn)
            check = restore_check_due(status, settings.backup_restore_check_days, now())
            result = run_backup(config, restore_check=check, should_stop=stop.is_set)
            log.info("backup %s done: %s", result.run_id, result.summary())
            due = None
        except BackupError as exc:
            log.error("backup failed: %s", exc)
            if retries < MAX_RETRIES and not stop.is_set():
                retries += 1
                due = now() + timedelta(seconds=RETRY_AFTER_S)
            else:
                due = None
        except Exception:
            log.exception("backup run crashed")
            due = None
    log.info("backup service stopped")


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("received %s; stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
