"""``coach backup schedule``: the backup service of the Compose stack.

* One backup every day at ``COACH_BACKUP_AT`` (local time, ``COACH_TZ``).
* Right after start, when the last successful backup is missing or older than
  :data:`CATCH_UP_AFTER`, one backup after :data:`START_DELAY_S` (a new deployment gets its
  first backup the same day; a server that was down catches up).
* A failed run is retried after :data:`RETRY_AFTER_S`, at most :data:`MAX_RETRIES` times, then
  the next scheduled time is awaited.
* When the last restore check is older than ``COACH_BACKUP_RESTORE_CHECK_DAYS``, the run
  also restores its dump into a scratch database.
* SIGTERM or SIGINT: no new run starts; a running one stops at its next step.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from thresy import db

from .core import BackupConfig, BackupError, get_status, run_backup
from .settings import BackupSettings

log = logging.getLogger(__name__)

START_DELAY_S = 60.0
CATCH_UP_AFTER = timedelta(hours=26)
RETRY_AFTER_S = 3600.0
MAX_RETRIES = 2


def next_time(now: datetime, settings: BackupSettings, zone: ZoneInfo) -> datetime:
    """The next ``COACH_BACKUP_AT`` after ``now`` (aware datetimes)."""
    local = now.astimezone(zone)
    candidate = datetime.combine(local.date(), settings.at, tzinfo=zone)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), settings.at, tzinfo=zone)
    return candidate.astimezone(UTC)


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
    return last is None or now - last >= timedelta(days=days)


def catch_up_due(status: dict, now: datetime) -> bool:
    last = _parse(status.get("last_success_at"))
    return last is None or now - last >= CATCH_UP_AFTER


def run_forever(
    config: BackupConfig,
    settings: BackupSettings,
    zone: ZoneInfo,
    *,
    stop: threading.Event | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    stop = stop or threading.Event()
    with db.connect(config.database_url) as conn:
        status = get_status(conn)
    due = now() + timedelta(seconds=START_DELAY_S) if catch_up_due(status, now()) else None
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
