"""``coach sync poll``: the long-running sync service.

* Only one poller works at a time: it holds a Postgres advisory lock (:data:`POLLER_LOCK`) on
  its connection. A second poller waits in standby and takes over when the lock is free.
* A pass runs every ``interval_s`` (plus or minus up to ``jitter_s``); inside the morning
  window (``morning_start``..``morning_end``, local time) it runs every ``morning_interval_s``
  while today's sleep has not arrived. After a 429 the next pass waits for the back-off.
* Between passes it checks for ``sync_now`` requests every ``request_check_s`` and serves
  them at once with one pass.
* SIGTERM or SIGINT: the current step of a running pass finishes, the pass stops, the loop
  ends and the process exits 0.
* A failing pass never ends the loop; its error is recorded in ``sync_state``. A lost database
  connection is retried after ``db_retry_s``.

The clock, the sleep function and the random source are injectable, so tests run the loop
without waiting.
"""

from __future__ import annotations

import contextlib
import logging
import random
import signal
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, time, timedelta
from typing import Any

import psycopg

from thresy.store import Conn
from thresy.sync import agent_requests, state
from thresy.sync.config import SyncConfig
from thresy.sync.passes import ClientSource, SyncOutcome, local_today

log = logging.getLogger(__name__)

POLLER_LOCK = 0x5448_5359  # "THSY": one sync poller (or `coach sync once`) per database


def try_lock(conn: Conn, key: int = POLLER_LOCK) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()
    return bool(row and row["ok"])


def unlock(conn: Conn, key: int = POLLER_LOCK) -> None:
    with contextlib.suppress(psycopg.Error):
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def lock_held(conn: Conn, key: int = POLLER_LOCK) -> bool:
    """Whether any session of this database holds the lock (no lock is taken)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database"
        " WHERE l.locktype = 'advisory' AND l.granted AND d.datname = current_database()"
        " AND l.classid = %s AND l.objid = %s AND l.objsubid = 1) AS held",
        ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
    ).fetchone()
    return bool(row and row["held"])


@contextlib.contextmanager
def holding_lock(conn: Conn, key: int = POLLER_LOCK) -> Iterator[bool]:
    """``with holding_lock(conn) as ok:``; ``ok`` is False when another session holds it."""
    ok = try_lock(conn, key)
    try:
        yield ok
    finally:
        if ok:
            unlock(conn, key)


def in_window(now_local: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_local < end
    return now_local >= start or now_local < end  # window across midnight


def sleep_synced(conn: Conn, day: Any) -> bool:
    row = conn.execute(
        "SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %s", (day,)
    ).fetchone()
    return bool(row and row["sleep_synced_at"] is not None)


class Poller:
    """The sync loop. ``connect`` opens a ``coach_app`` connection; ``provider`` hands out
    the Garmin client (:class:`thresy.sync.auth.ClientProvider`)."""

    def __init__(
        self,
        config: SyncConfig,
        *,
        connect: Callable[[], Conn],
        provider: ClientSource,
        clock: Callable[[], datetime] = state.utc_now,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        handled_by: str | None = None,
        max_passes: int | None = None,
    ) -> None:
        self.config = config
        self.connect = connect
        self.provider = provider
        self.clock = clock
        self._stop = threading.Event()
        self._sleep_fn = sleep
        self.rng = rng or random.Random()
        self.handled_by = handled_by or agent_requests.handler_name()
        self.max_passes = max_passes
        self.passes = 0
        self.last_outcome: SyncOutcome | None = None

    # Stopping ---------------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self, *_: Any) -> None:
        """Ask the loop to end after the current step (also the SIGTERM/SIGINT handler)."""
        if not self._stop.is_set():
            log.info("stop requested; finishing the current step")
        self._stop.set()

    def sleep(self, seconds: float) -> None:
        if self.stopping or seconds <= 0:
            return
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
        else:
            self._stop.wait(seconds)

    @contextlib.contextmanager
    def _signals(self, install: bool) -> Iterator[None]:
        previous: dict[int, Any] = {}
        if install and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, self.stop)
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    # Loop -------------------------------------------------------------------------------

    def run(self, *, install_signals: bool = True) -> int:
        """Run until stopped; returns the process exit code (0)."""
        with self._signals(install_signals):
            waiting_logged = False
            while not self.stopping:
                try:
                    with self.connect() as conn, holding_lock(conn) as locked:
                        if not locked:
                            if not waiting_logged:
                                log.info("another sync poller is running; standing by")
                                waiting_logged = True
                            self.sleep(self.config.lock_retry_s)
                            continue
                        waiting_logged = False
                        log.info("sync poller started")
                        self._serve(conn)
                except psycopg.OperationalError as exc:
                    log.warning("database connection lost (%s); retrying", exc)
                    self.sleep(self.config.db_retry_s)
        log.info("sync poller stopped")
        return 0

    def _serve(self, conn: Conn) -> None:
        orphans = agent_requests.fail_orphans(conn)
        if orphans:
            log.warning("%d sync request(s) left over from a previous run marked failed", orphans)
        next_at = self.clock()
        while not self.stopping:
            now = self.clock()
            if now >= next_at or agent_requests.has_pending(conn):
                self._cycle(conn, now)
                if self.max_passes is not None and self.passes >= self.max_passes:
                    self.stop()
                    break
                next_at = self.next_pass_at(conn, self.clock())
                continue
            self.sleep(min(self.config.request_check_s, (next_at - now).total_seconds()))

    def _cycle(self, conn: Conn, now: datetime) -> None:
        outcome, ids = agent_requests.sync_and_answer(
            conn,
            self.config,
            self.provider,
            now=now,
            handled_by=self.handled_by,
            should_stop=lambda: self.stopping,
        )
        self.passes += 1
        self.last_outcome = outcome
        served = f" (answering request {', '.join(map(str, ids))})" if ids else ""
        if outcome.ok and outcome.result is not None:
            log.info("sync pass done%s: %s", served, outcome.result.summary())
        elif outcome.skipped:
            log.info("sync pass skipped%s: %s", served, outcome.error)
        else:
            log.warning("sync pass failed%s: %s", served, outcome.error)

    def next_pass_at(self, conn: Conn, now: datetime) -> datetime:
        """When the next scheduled pass is due after a pass that ended at ``now``."""
        config = self.config
        interval, jitter = config.interval_s, config.jitter_s
        last = self.last_outcome
        auth_problem = last is not None and last.error_kind == "auth"
        local = now.astimezone(config.zone)
        if (
            not auth_problem
            and in_window(local.time(), config.morning_start, config.morning_end)
            and not sleep_synced(conn, local_today(now, config))
        ):
            interval = config.morning_interval_s
            jitter = min(jitter, interval / 5)
        delay = max(config.request_check_s, interval + self.rng.uniform(-jitter, jitter))
        next_at = now + timedelta(seconds=delay)
        until = state.rate_limited_until(conn)
        if until is not None and until > next_at:
            next_at = until
        return next_at
