"""``coach sync backfill``: import history from Garmin, slowly and resumably, without events.

* Activities are listed in chunks of ``backfill_chunk_days``; every activity is upserted and,
  with ``fit``, its original file is downloaded unless the activity already has records or a
  file (or the download failed for good earlier).
* Wellness (with ``wellness``) is fetched day by day; days that are already complete (sleep
  synced, HRV present, final step count) are skipped without a request.
* Before every Garmin request the backfill pauses ``backfill_pause_s``. On HTTP 429 it waits
  with exponential back-off and retries the request; after ``backfill_max_rate_limits``
  rate limits in a row it stops, to be resumed later.
* Progress is stored in ``sync_state["backfill"]`` after every chunk and day. Running the same
  command again continues where it stopped; ``restart`` starts over. Everything is idempotent.
* No events are raised: neither ``new_activity`` nor ``sleep_arrived``.
* ``until`` is at most yesterday (local date): today belongs to the poller, which raises the
  events for it.
"""

from __future__ import annotations

import logging
import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from thresy import store
from thresy.garmin.client import GarminClient
from thresy.garmin.errors import GarminAuthError, GarminError, GarminRateLimited
from thresy.store import Conn
from thresy.sync import state
from thresy.sync.config import SyncConfig
from thresy.sync.passes import (
    SOURCE,
    WELLNESS_KEEP_DAYS,
    activities_missing_fit,
    download_fit,
    fetch_wellness,
    fit_given_up,
    incomplete_parts,
    local_today,
    never,
    prune_fit_failures,
    wellness_rows,
)

log = logging.getLogger(__name__)

BACKFILL_LOCK = 0x5448_4246  # "THBF": one backfill at a time


class BackfillStopped(RuntimeError):
    """Garmin kept rate-limiting; progress is saved, run the same command later."""


@dataclass
class BackfillResult:
    since: date
    until: date
    resumed: bool = False
    activities: int = 0
    new_activities: int = 0
    fit_stored: int = 0
    fit_failed: int = 0
    wellness_days: int = 0
    wellness_skipped: int = 0
    requests: int = 0
    finished: bool = False
    stopped_early: bool = False
    warnings: list[str] = field(default_factory=list)


class _Polite:
    """Runs Garmin requests with a pause before each and back-off on rate limits."""

    def __init__(
        self, config: SyncConfig, sleep: Callable[[float], None], result: BackfillResult
    ) -> None:
        self.config = config
        self.sleep = sleep
        self.result = result

    def __call__(self, func: Callable[..., Any], *args: Any) -> Any:
        limited = 0
        while True:
            if self.config.backfill_pause_s:
                self.sleep(self.config.backfill_pause_s)
            self.result.requests += 1
            try:
                return func(*args)
            except GarminRateLimited as exc:
                limited += 1
                if limited >= self.config.backfill_max_rate_limits:
                    raise BackfillStopped(
                        f"Garmin rate-limited {limited} requests in a row ({exc}); progress is "
                        "saved, run the same command later to continue"
                    ) from exc
                wait = min(
                    self.config.rate_limit_backoff_max_s,
                    self.config.rate_limit_backoff_s * 2 ** (limited - 1),
                )
                log.warning("rate limited; waiting %.0f s before retrying", wait)
                self.sleep(wait)


def _params(since: date, until: date, wellness: bool, fit: bool) -> dict[str, Any]:
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "wellness": wellness,
        "fit": fit,
    }


def _missing_parts(
    day: date, row: dict[str, Any] | None, info: dict[str, Any], config: SyncConfig
) -> set[str]:
    """Like :func:`~thresy.sync.passes.incomplete_parts`, but a stored step count counts as
    final for days the poller has no fetch record of (older history)."""
    parts = incomplete_parts(day, row, info, config)
    if "stats" in parts and not info and row is not None and row["steps"] is not None:
        parts.discard("stats")
    return parts


def clamp_until(until: date | None, today: date) -> date:
    """The last day a backfill may touch: ``until``, but at most yesterday."""
    yesterday = today - timedelta(days=1)
    return yesterday if until is None or until > yesterday else until


def backfill(
    conn: Conn,
    client: GarminClient,
    config: SyncConfig,
    *,
    since: date,
    until: date | None = None,
    wellness: bool = True,
    fit: bool = True,
    restart: bool = False,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time_mod.sleep,
    should_stop: Callable[[], bool] = never,
) -> BackfillResult:
    """Import ``since``..``until`` (inclusive; see the module docstring)."""
    now = now or state.utc_now()
    until = clamp_until(until, local_today(now, config))
    result = BackfillResult(since=since, until=until)
    if since > until:
        result.finished = True
        return result
    params = _params(since, until, wellness, fit)
    job = state.get(conn, state.BACKFILL)
    if job.get("params") == params and not job.get("finished_at") and not restart:
        result.resumed = True
    else:
        job = {"params": params, "started_at": state.iso(now)}
    job["updated_at"] = state.iso(now)
    state.put(conn, state.BACKFILL, job)
    call = _Polite(config, sleep, result)

    def save(**changes: Any) -> None:
        job.update(changes, updated_at=state.iso(state.utc_now()))
        state.put(conn, state.BACKFILL, job)

    _activities(conn, client, config, job, result, call, fit, now, save, should_stop)
    if wellness and not result.stopped_early:
        _wellness(conn, client, config, job, result, call, now, save, should_stop)
    if not result.stopped_early:
        result.finished = True
        save(finished_at=state.iso(state.utc_now()))
    return result


def _activities(
    conn: Conn,
    client: GarminClient,
    config: SyncConfig,
    job: dict[str, Any],
    result: BackfillResult,
    call: _Polite,
    fit: bool,
    now: datetime,
    save: Callable[..., None],
    should_stop: Callable[[], bool],
) -> None:
    done = job.get("activities_done_through")
    start = date.fromisoformat(done) + timedelta(days=1) if done else result.since
    failures = state.get(conn, state.FIT_FAILURES)
    while start <= result.until:
        if should_stop():
            result.stopped_early = True
            return
        end = min(result.until, start + timedelta(days=config.backfill_chunk_days - 1))
        summaries = call(client.get_activities_by_date, start, end)
        ids = []
        for summary in summaries:
            try:
                activity_id = store.activity_row(summary)["id"]
            except (ValueError, TypeError) as exc:
                result.warnings.append(f"skipped an activity: {exc}")
                continue
            ids.append(activity_id)
            result.activities += 1
            if store.upsert_activity(conn, summary, source=SOURCE):
                result.new_activities += 1
        if fit:
            for row in activities_missing_fit(conn, ids):
                if fit_given_up(failures.get(str(row["id"])), config):
                    continue
                if should_stop():
                    result.stopped_early = True
                    return
                try:
                    error = download_fit(conn, client, config, row, failures, now, call)
                finally:
                    state.put(conn, state.FIT_FAILURES, prune_fit_failures(failures, now))
                if error is None:
                    result.fit_stored += 1
                else:
                    result.fit_failed += 1
        save(activities_done_through=end.isoformat())
        start = end + timedelta(days=1)


def _wellness(
    conn: Conn,
    client: GarminClient,
    config: SyncConfig,
    job: dict[str, Any],
    result: BackfillResult,
    call: _Polite,
    now: datetime,
    save: Callable[..., None],
    should_stop: Callable[[], bool],
) -> None:
    done = job.get("wellness_done_through")
    day = date.fromisoformat(done) + timedelta(days=1) if done else result.since
    while day <= result.until:
        if should_stop():
            result.stopped_early = True
            return
        fetched = state.get(conn, state.WELLNESS)
        info = dict(fetched.get(day.isoformat()) or {})
        parts = _missing_parts(day, wellness_rows(conn, [day]).get(day), info, config)
        if parts:
            try:
                payloads = fetch_wellness(client, day, parts, call)
            except (GarminRateLimited, GarminAuthError):
                raise
            except GarminError as exc:  # e.g. a day before the account existed
                result.warnings.append(f"{day.isoformat()}: {exc}")
                payloads = None
            if payloads is not None:
                try:
                    store.upsert_wellness(conn, day, synced_at=now, **payloads)
                    result.wellness_days += 1
                except ValueError as exc:
                    result.warnings.append(f"{day.isoformat()}: {exc}")
                info["checked_at"] = state.iso(now)
                if store.is_ok_payload(payloads.get("stats")):
                    info["stats_at"] = state.iso(now)
                # Recent days stay in wellness_fetched for the poller; older ones need no entry.
                if day >= local_today(now, config) - timedelta(days=WELLNESS_KEEP_DAYS):
                    fetched[day.isoformat()] = info
                    state.put(conn, state.WELLNESS, fetched)
        else:
            result.wellness_skipped += 1
        save(wellness_done_through=day.isoformat())
        day += timedelta(days=1)
