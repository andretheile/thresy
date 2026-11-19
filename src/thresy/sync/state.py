"""What the sync remembers in ``sync_state``, and its events.

Keys (values are JSON; times are ISO 8601 UTC strings):

``sync_status``
    ``last_attempt_at``, ``last_success_at``, ``last_error``, ``last_error_kind``
    (``auth`` | ``rate_limit`` | ``garmin`` | ``error``), ``last_error_at``,
    ``consecutive_failures``, ``last_result`` (the ``sync_now`` result of the last good pass).
    This answers "when did we last sync?".
``garmin_auth``
    ``status`` (``ok`` | ``no_credentials`` | ``mfa_required`` | ``login_failed`` |
    ``login_paused``), ``reason``, ``updated_at``, ``last_ok_at``,
    ``last_password_login_at``, ``password_login_paused_until``.
``garmin_rate_limit``
    ``until``, ``backoff_s`` (exponential back-off after HTTP 429).
``wellness_fetched``
    per wake date: ``checked_at`` (last fetch) and ``stats_at`` (last fetch of the daily stats,
    to know whether the step count is final).
``activities_pending``
    activity id -> first seen; new activities whose ``new_activity`` event waits for the FIT
    file.
``fit_failures``
    activity id -> ``attempts``, ``error``, ``permanent``, ``at``.
``personal_records_at``
    last refresh of the personal records.
``backfill``
    progress of ``coach sync backfill`` (see :mod:`thresy.sync.backfill`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from thresy import store
from thresy.store import Conn

STATUS = "sync_status"
AUTH = "garmin_auth"
RATE_LIMIT = "garmin_rate_limit"
WELLNESS = "wellness_fetched"
PENDING = "activities_pending"
FIT_FAILURES = "fit_failures"
PERSONAL_RECORDS = "personal_records_at"
BACKFILL = "backfill"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def get(conn: Conn, key: str) -> dict[str, Any]:
    """The dict stored under ``key`` (empty when unset or not a dict)."""
    value = store.get_sync_state(conn, key)
    return dict(value) if isinstance(value, Mapping) else {}


def put(conn: Conn, key: str, value: Mapping[str, Any]) -> None:
    store.set_sync_state(conn, key, dict(value))


def merge(conn: Conn, key: str, **changes: Any) -> dict[str, Any]:
    """Update some fields of the dict under ``key``; returns the new value."""
    with conn.transaction():
        value = get(conn, key)
        value.update(changes)
        put(conn, key, value)
    return value


# Pass bookkeeping -----------------------------------------------------------------------------


def record_attempt(conn: Conn, now: datetime) -> None:
    merge(conn, STATUS, last_attempt_at=iso(now))


def record_success(conn: Conn, now: datetime, result: Mapping[str, Any]) -> None:
    merge(
        conn,
        STATUS,
        last_success_at=iso(now),
        consecutive_failures=0,
        last_result=dict(result),
    )


def record_failure(conn: Conn, now: datetime, kind: str, error: str) -> int:
    """Store the error; returns the number of failed passes in a row."""
    with conn.transaction():
        value = get(conn, STATUS)
        failures = int(value.get("consecutive_failures") or 0) + 1
        value.update(
            last_error=error,
            last_error_kind=kind,
            last_error_at=iso(now),
            consecutive_failures=failures,
        )
        put(conn, STATUS, value)
    return failures


# Rate limits ----------------------------------------------------------------------------------


def rate_limited_until(conn: Conn) -> datetime | None:
    return parse_time(get(conn, RATE_LIMIT).get("until"))


def note_rate_limit(conn: Conn, now: datetime, *, base_s: float, max_s: float) -> datetime:
    """Start or double the back-off after a 429; returns when the next request may go out."""
    with conn.transaction():
        value = get(conn, RATE_LIMIT)
        previous = float(value.get("backoff_s") or 0)
        backoff = min(max_s, previous * 2 if previous else base_s)
        until = now + timedelta(seconds=backoff)
        put(conn, RATE_LIMIT, {"until": iso(until), "backoff_s": backoff, "since": iso(now)})
    return until


def clear_rate_limit(conn: Conn) -> None:
    if get(conn, RATE_LIMIT):
        put(conn, RATE_LIMIT, {})


# Events ---------------------------------------------------------------------------------------


def insert_auth_required(conn: Conn, now: datetime, reason: str) -> int | None:
    """``garmin_auth_required``, at most one per UTC day (``garmin_auth:<date>``)."""
    day = now.astimezone(UTC).date().isoformat()
    return store.insert_event(
        conn, "garmin_auth_required", {"reason": reason}, dedupe_key=f"garmin_auth:{day}"
    )


def insert_sync_failed(conn: Conn, now: datetime, error: str) -> int | None:
    """``sync_failed``, at most one per UTC hour (``sync_failed:<date>:<hour>``)."""
    utc = now.astimezone(UTC)
    key = f"sync_failed:{utc.date().isoformat()}:{utc.hour:02d}"
    return store.insert_event(conn, "sync_failed", {"error": error}, dedupe_key=key)
