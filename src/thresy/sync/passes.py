"""One sync pass: activities, wellness and personal records from Garmin into Postgres.

:func:`run_pass` does the work with a logged-in client; :func:`sync_once` wraps it with the
login, error handling, rate-limit back-off and the bookkeeping in ``sync_state``. Both the
poller and ``coach sync once`` use :func:`sync_once`.

A pass:

1. **Activities.** Lists the activities of the last ``lookback_days`` local days and upserts
   each. For every listed activity without records or file, downloads the original, saves the
   FIT file (``fit/<YYYY>/<id>.fit``) and stores records and laps. A failed download is retried
   on later passes, at most ``fit_max_attempts`` times; a missing file (404) is not retried.
2. **Wellness.** For today's local date (``COACH_TZ``) always sleep, HRV and daily stats; for
   yesterday and up to ``recheck_days`` older days only what is still incomplete: sleep not
   synced, no HRV, or a step count fetched before the day was over. Older days are rechecked
   at most every ``recheck_interval_s``.
3. **Personal records** every ``pr_refresh_h`` and after each new run.

Events (only with ``raise_events``; never from the importer or a backfill):

* ``sleep_arrived`` when real sleep for *today's* wake date is stored for the first time, in
  the same transaction as the sleep itself, so it cannot be lost or doubled.
* ``new_activity`` for an activity first inserted by this pass, once its FIT file is stored or
  given up, and only if it started within ``new_activity_max_age_h``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

import psycopg

from thresy import store
from thresy.fit import FitError
from thresy.garmin.client import GarminClient, none_if_empty
from thresy.garmin.errors import (
    GarminAuthError,
    GarminAuthRequired,
    GarminError,
    GarminNotFound,
    GarminRateLimited,
)
from thresy.logs import redact
from thresy.store import Conn
from thresy.sync import state
from thresy.sync.config import SyncConfig
from thresy.sync.fitfiles import store_original

log = logging.getLogger(__name__)

SOURCE = "garmin"
WELLNESS_KEEP_DAYS = 45  # wellness_fetched entries older than this are dropped
FIT_FAILURES_KEEP_DAYS = 90


def never() -> bool:
    return False


Call = Callable[..., Any]


def _direct(func: Call, *args: Any) -> Any:
    return func(*args)


@dataclass
class PassResult:
    """What one pass did. :meth:`request_result` is the ``sync_now`` result."""

    at: datetime
    wake_date: date  # today's local date: the wake date of last night
    activities_seen: int = 0
    new_activities: list[int] = field(default_factory=list)
    fit_stored: list[int] = field(default_factory=list)
    fit_failed: dict[int, str] = field(default_factory=dict)
    wellness_days: list[date] = field(default_factory=list)
    sleep_arrived: bool = False
    sleep_synced: bool = False
    events: list[str] = field(default_factory=list)  # dedupe keys of the events raised
    personal_records: int | None = None
    warnings: list[str] = field(default_factory=list)
    stopped_early: bool = False

    def request_result(self) -> dict[str, Any]:
        return {
            "new_activities": list(self.new_activities),
            "sleep_synced": self.sleep_synced,
            "wake_date": self.wake_date.isoformat(),
        }

    def summary(self) -> str:
        parts = [f"{self.activities_seen} activities checked"]
        if self.new_activities:
            parts.append(f"{len(self.new_activities)} new")
        if self.fit_stored:
            parts.append(f"{len(self.fit_stored)} FIT files stored")
        if self.fit_failed:
            parts.append(f"{len(self.fit_failed)} FIT downloads failed")
        days = ", ".join(d.isoformat() for d in self.wellness_days) or "none"
        parts.append(f"wellness fetched for {days}")
        night = "synced" if self.sleep_synced else "not synced yet"
        parts.append(f"sleep of {self.wake_date.isoformat()} {night}")
        if self.events:
            parts.append(f"events: {', '.join(self.events)}")
        if self.stopped_early:
            parts.append("stopped early")
        return "; ".join(parts)


# Shared helpers (also used by the backfill) ---------------------------------------------------


def local_today(now: datetime, config: SyncConfig) -> date:
    return now.astimezone(config.zone).date()


def _not_found_is_none(call: Call, func: Call, *args: Any) -> Any:
    try:
        return call(func, *args)
    except GarminNotFound:
        return None


def fetch_wellness(
    client: GarminClient, day: date, parts: set[str], call: Call = _direct
) -> dict[str, Any]:
    """Fetch the wanted ``parts`` (``sleep``, ``hrv``, ``stats``) of ``day``.

    The 15-minute step intervals are fetched only when the daily stats have no step count.
    ``call(func, *args)`` runs each request (the backfill uses it to pause between requests).
    """
    out: dict[str, Any] = {}
    if "sleep" in parts:
        out["sleep"] = none_if_empty(_not_found_is_none(call, client.get_sleep_data, day))
    if "hrv" in parts:
        out["hrv"] = none_if_empty(_not_found_is_none(call, client.get_hrv_data, day))
    if "stats" in parts:
        stats = none_if_empty(_not_found_is_none(call, client.get_stats, day))
        out["stats"] = stats
        if not (store.is_ok_payload(stats) and stats.get("totalSteps") is not None):
            out["steps"] = none_if_empty(_not_found_is_none(call, client.get_steps_data, day))
    return out


def wellness_rows(conn: Conn, days: list[date]) -> dict[date, dict[str, Any]]:
    rows = conn.execute(
        "SELECT wake_date, sleep_synced_at, raw_hrv IS NOT NULL AS has_hrv, steps"
        " FROM wellness_daily WHERE wake_date = ANY(%s)",
        (days,),
    ).fetchall()
    return {r["wake_date"]: r for r in rows}


def stats_final(day: date, info: Mapping[str, Any], config: SyncConfig) -> bool:
    """True when the daily stats of ``day`` were fetched late enough to be complete."""
    fetched = state.parse_time(info.get("stats_at"))
    day_end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=config.zone)
    return fetched is not None and fetched >= day_end + timedelta(hours=config.steps_final_after_h)


def incomplete_parts(
    day: date, row: Mapping[str, Any] | None, info: Mapping[str, Any], config: SyncConfig
) -> set[str]:
    """The parts of a past day that are still worth fetching."""
    parts = set()
    if row is None or row["sleep_synced_at"] is None:
        parts.add("sleep")
    if row is None or not row["has_hrv"]:
        parts.add("hrv")
    if not stats_final(day, info, config):
        parts.add("stats")
    return parts


def prune_days(fetched: dict[str, Any], today: date, keep_days: int) -> dict[str, Any]:
    oldest = (today - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in fetched.items() if k >= oldest}


def fit_given_up(failure: Mapping[str, Any] | None, config: SyncConfig) -> bool:
    if not failure:
        return False
    return bool(failure.get("permanent")) or int(failure.get("attempts") or 0) >= (
        config.fit_max_attempts
    )


def activities_missing_fit(conn: Conn, ids: list[int]) -> list[dict[str, Any]]:
    """Of ``ids``, the activities with neither records nor a stored file, newest first."""
    if not ids:
        return []
    return conn.execute(
        "SELECT id, start_local, local_date FROM activities"
        " WHERE id = ANY(%s) AND NOT has_records AND fit_path IS NULL"
        " ORDER BY start_utc DESC NULLS LAST, id DESC",
        (ids,),
    ).fetchall()


def download_fit(
    conn: Conn,
    client: GarminClient,
    config: SyncConfig,
    row: Mapping[str, Any],
    failures: dict[str, Any],
    now: datetime,
    call: Call = _direct,
) -> str | None:
    """Download, save and parse one activity's original file.

    Returns ``None`` on success, otherwise the error (also recorded in ``failures``, a
    ``fit_failures`` map the caller saves). Rate limits and auth errors propagate.
    """
    activity_id = int(row["id"])
    key = str(activity_id)
    permanent = False
    try:
        original = call(client.download_original, activity_id)
        store_original(
            conn, config.data_dir, activity_id, row["start_local"] or row["local_date"], original
        )
    except (GarminRateLimited, GarminAuthError):
        raise
    except GarminNotFound:
        error, permanent = "Garmin has no original file for this activity", True
    except FitError as exc:
        error, permanent = f"unusable original file: {exc}", True
    except GarminError as exc:
        error = f"download failed: {exc}"
    else:
        failures.pop(key, None)
        return None
    previous = failures.get(key) or {}
    failures[key] = {
        "attempts": int(previous.get("attempts") or 0) + 1,
        "error": error,
        "permanent": permanent,
        "at": state.iso(now),
    }
    log.warning("activity %s: %s", activity_id, error)
    return error


def prune_fit_failures(failures: dict[str, Any], now: datetime) -> dict[str, Any]:
    oldest = now - timedelta(days=FIT_FAILURES_KEEP_DAYS)
    return {
        k: v
        for k, v in failures.items()
        if (at := state.parse_time(v.get("at"))) is None or at >= oldest
    }


# The pass -------------------------------------------------------------------------------------


class _Pass:
    def __init__(
        self,
        conn: Conn,
        client: GarminClient,
        config: SyncConfig,
        now: datetime,
        raise_events: bool,
        should_stop: Callable[[], bool],
    ) -> None:
        self.conn = conn
        self.client = client
        self.config = config
        self.now = now
        self.today = local_today(now, config)
        self.raise_events = raise_events
        self.should_stop = should_stop
        self.result = PassResult(at=now, wake_date=self.today)
        self.new_runs = 0

    def stop_requested(self) -> bool:
        if self.should_stop():
            self.result.stopped_early = True
            return True
        return False

    # Activities -------------------------------------------------------------------------

    def activities(self) -> None:
        conn, config = self.conn, self.config
        start = self.today - timedelta(days=config.lookback_days)
        # One day beyond today: Garmin filters by the activity's own local date.
        summaries = self.client.get_activities_by_date(start, self.today + timedelta(days=1))
        self.result.activities_seen = len(summaries)
        pending = state.get(conn, state.PENDING)
        ids: list[int] = []
        for summary in summaries:
            try:
                row = store.activity_row(summary)
            except (ValueError, TypeError) as exc:
                self.result.warnings.append(f"skipped an activity: {exc}")
                continue
            ids.append(row["id"])
            with conn.transaction():
                new = store.upsert_activity(conn, summary, source=SOURCE)
                if new and self.raise_events:
                    pending[str(row["id"])] = state.iso(self.now)
                    state.put(conn, state.PENDING, pending)
            if new:
                self.result.new_activities.append(row["id"])
                self.new_runs += bool(row["is_run"])

        failures = state.get(conn, state.FIT_FAILURES)
        for row in activities_missing_fit(conn, ids):
            if fit_given_up(failures.get(str(row["id"])), config):
                continue
            if self.stop_requested():
                break
            try:
                error = download_fit(conn, self.client, config, row, failures, self.now)
            finally:
                state.put(conn, state.FIT_FAILURES, prune_fit_failures(failures, self.now))
            if error is None:
                self.result.fit_stored.append(int(row["id"]))
            else:
                self.result.fit_failed[int(row["id"])] = error

        if pending and self.raise_events:
            self.flush_activity_events(pending, failures)

    def _recent(self, row: Mapping[str, Any]) -> bool:
        oldest = self.now - timedelta(hours=self.config.new_activity_max_age_h)
        if row["start_utc"] is not None:
            return row["start_utc"] >= oldest
        return row["local_date"] >= oldest.astimezone(self.config.zone).date()

    def flush_activity_events(self, pending: dict[str, Any], failures: dict[str, Any]) -> None:
        """Raise ``new_activity`` for pending activities whose file is settled."""
        conn = self.conn
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, type, is_run, start_utc, local_date, has_records, fit_path"
                " FROM activities WHERE id = ANY(%s)",
                ([int(k) for k in pending],),
            ).fetchall()
        }
        max_age = timedelta(hours=self.config.new_activity_max_age_h)
        for key, first_seen in list(pending.items()):
            row = rows.get(int(key))
            seen_at = state.parse_time(first_seen) or self.now
            if row is not None:
                settled = bool(row["has_records"] or row["fit_path"]) or fit_given_up(
                    failures.get(key), self.config
                )
                if not settled and self.now - seen_at < max_age:
                    continue  # wait for the file: the debrief wants the records
            with conn.transaction():
                if (
                    row is not None
                    and self._recent(row)
                    and store.insert_new_activity(conn, row["id"], row["type"], row["is_run"])
                ):
                    self.result.events.append(f"activity:{row['id']}")
                del pending[key]
                state.put(conn, state.PENDING, pending)

    # Wellness ---------------------------------------------------------------------------

    def wellness(self) -> None:
        conn, config, today = self.conn, self.config, self.today
        days = [today - timedelta(days=k) for k in range(2 + config.recheck_days)]
        rows = wellness_rows(conn, days)
        fetched = state.get(conn, state.WELLNESS)
        recheck = timedelta(seconds=config.recheck_interval_s)
        for day in days:
            info = dict(fetched.get(day.isoformat()) or {})
            if day == today:
                parts = {"sleep", "hrv", "stats"}
            else:
                checked = state.parse_time(info.get("checked_at"))
                if day < today - timedelta(days=1) and checked and self.now - checked < recheck:
                    continue
                parts = incomplete_parts(day, rows.get(day), info, config)
            if not parts:
                continue
            if self.stop_requested():
                break
            payloads = fetch_wellness(self.client, day, parts)
            self.store_wellness(day, payloads)
            info["checked_at"] = state.iso(self.now)
            if store.is_ok_payload(payloads.get("stats")):
                info["stats_at"] = state.iso(self.now)
            fetched[day.isoformat()] = info
            state.put(conn, state.WELLNESS, prune_days(fetched, today, WELLNESS_KEEP_DAYS))

    def store_wellness(self, day: date, payloads: dict[str, Any]) -> None:
        conn = self.conn
        try:
            with conn.transaction():
                res = store.upsert_wellness(conn, day, synced_at=self.now, **payloads)
                event = None
                if res.sleep_arrived and day == self.today and self.raise_events:
                    event = store.insert_sleep_arrived(conn, day)
        except ValueError as exc:  # a payload for another calendar day
            self.result.warnings.append(f"{day.isoformat()}: {exc}")
            log.warning("wellness %s not stored: %s", day.isoformat(), exc)
            return
        self.result.wellness_days.append(day)
        if res.sleep_arrived and day == self.today:
            self.result.sleep_arrived = True
        if event:
            self.result.events.append(f"sleep:{day.isoformat()}")

    # Personal records -------------------------------------------------------------------

    def personal_records(self) -> None:
        last = state.parse_time(state.get(self.conn, state.PERSONAL_RECORDS).get("at"))
        due = last is None or self.now - last >= timedelta(hours=self.config.pr_refresh_h)
        if not (due or self.new_runs):
            return
        try:
            payload = self.client.get_personal_records()
        except (GarminRateLimited, GarminAuthError):
            raise
        except GarminError as exc:
            self.result.warnings.append(f"personal records not refreshed: {exc}")
            return
        records = store.garmin_personal_records(payload)
        self.result.personal_records = store.upsert_personal_records(self.conn, records)
        state.put(self.conn, state.PERSONAL_RECORDS, {"at": state.iso(self.now)})

    def run(self) -> PassResult:
        self.activities()
        if not self.stop_requested():
            self.wellness()
        if not self.stop_requested():
            self.personal_records()
        row = self.conn.execute(
            "SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %s", (self.today,)
        ).fetchone()
        self.result.sleep_synced = bool(row and row["sleep_synced_at"] is not None)
        return self.result


def run_pass(
    conn: Conn,
    client: GarminClient,
    config: SyncConfig,
    *,
    now: datetime | None = None,
    raise_events: bool = True,
    should_stop: Callable[[], bool] = never,
) -> PassResult:
    """One pass with a logged-in ``client`` (see the module docstring).

    ``now`` (aware, default: the current time) fixes "today" and all timestamps.
    ``should_stop`` is checked between steps; when it returns true the pass ends early and
    ``stopped_early`` is set. Garmin errors other than "not found" end the pass with an
    exception; everything stored until then stays stored.
    """
    return _Pass(conn, client, config, now or state.utc_now(), raise_events, should_stop).run()


# With login and bookkeeping -------------------------------------------------------------------


@dataclass
class SyncOutcome:
    """Result of :func:`sync_once`."""

    ok: bool
    result: PassResult | None = None
    error: str | None = None
    error_kind: str | None = None  # auth | rate_limit | garmin | error
    skipped: bool = False  # not attempted (rate-limit back-off)


class ClientSource(Protocol):
    """What :func:`sync_once` needs from :class:`thresy.sync.auth.ClientProvider`."""

    def get(self, conn: Conn, now: datetime) -> GarminClient: ...

    def invalidate(self) -> None: ...


def _failed(
    conn: Conn, config: SyncConfig, now: datetime, kind: str, error: str, raise_events: bool
) -> SyncOutcome:
    error = redact(error)  # stored, raised as an event and shown to the athlete
    failures = state.record_failure(conn, now, kind, error)
    if raise_events and kind != "auth" and failures >= config.fail_event_after:
        state.insert_sync_failed(conn, now, error)
    return SyncOutcome(ok=False, error=error, error_kind=kind)


def sync_once(
    conn: Conn,
    config: SyncConfig,
    provider: ClientSource,
    *,
    now: datetime | None = None,
    raise_events: bool = True,
    should_stop: Callable[[], bool] = never,
) -> SyncOutcome:
    """Log in if needed, run one pass, and record the outcome in ``sync_state``.

    Never raises for Garmin problems or bugs in the pass (they become a failed outcome, and
    ``sync_failed`` after ``fail_event_after`` failures in a row); only a lost database
    connection (``psycopg.OperationalError``) propagates. ``provider`` is a
    :class:`thresy.sync.auth.ClientProvider` (or anything with its ``get``/``invalidate``).
    """
    now = now or state.utc_now()
    until = state.rate_limited_until(conn)
    if until is not None and now < until:
        error = f"Garmin is rate-limiting requests; next attempt after {until:%Y-%m-%d %H:%M} UTC"
        return SyncOutcome(ok=False, error=error, error_kind="rate_limit", skipped=True)
    state.record_attempt(conn, now)
    try:
        client = provider.get(conn, now)
        result = run_pass(
            conn, client, config, now=now, raise_events=raise_events, should_stop=should_stop
        )
    except GarminRateLimited as exc:
        until = state.note_rate_limit(
            conn, now, base_s=config.rate_limit_backoff_s, max_s=config.rate_limit_backoff_max_s
        )
        error = f"Garmin rate limit ({exc}); next attempt after {until:%Y-%m-%d %H:%M} UTC"
        return _failed(conn, config, now, "rate_limit", error, raise_events)
    except GarminAuthRequired as exc:
        provider.invalidate()
        return _failed(conn, config, now, "auth", exc.reason, raise_events)
    except GarminAuthError as exc:
        provider.invalidate()  # the next pass logs in again
        return _failed(conn, config, now, "auth", f"Garmin rejected the session: {exc}", False)
    except GarminError as exc:
        return _failed(conn, config, now, "garmin", f"Garmin request failed: {exc}", raise_events)
    except psycopg.OperationalError:
        raise
    except Exception as exc:
        log.exception("sync pass failed")
        return _failed(conn, config, now, "error", f"{type(exc).__name__}: {exc}", raise_events)
    if not result.stopped_early:
        state.record_success(conn, now, result.request_result())
        state.clear_rate_limit(conn)
    return SyncOutcome(ok=True, result=result)
