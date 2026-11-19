"""The poller loop without real waiting: injected clock, sleep and random source."""

from __future__ import annotations

import os
import random
import signal
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from tests import synthetic
from tests.sync.helpers import Clock, StaticProvider, activity_at, make_config
from thresy import db
from thresy.garmin.errors import GarminError, GarminRateLimited
from thresy.garmin.fake import FakeGarminClient
from thresy.sync import agent_requests, state
from thresy.sync.poller import POLLER_LOCK, Poller, in_window, lock_held

pytestmark = pytest.mark.db

DAY = date(2030, 1, 2)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2030, 1, 2, hour, minute, second, tzinfo=UTC)


class RecordingProvider(StaticProvider):
    def __init__(self, client) -> None:
        super().__init__(client)
        self.times: list[datetime] = []

    def get(self, conn, now):
        self.times.append(now)
        return super().get(conn, now)


class Harness:
    """A poller whose sleep advances the clock and runs per-sleep hooks."""

    def __init__(self, test_db, tmp_path, *, start: datetime, **config) -> None:
        self.clock = Clock(start)
        self.fake = FakeGarminClient()
        self.provider = RecordingProvider(self.fake)
        self.config = make_config(tmp_path, **config)
        self.sleeps: list[float] = []
        self.hooks: list = []
        self.url = test_db.app_url
        self.connects = 0

    def connect(self):
        self.connects += 1
        return db.connect(self.url)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        for hook in list(self.hooks):
            hook(self)

    def poller(self, **kwargs) -> Poller:
        kwargs.setdefault("rng", random.Random(1))
        return Poller(
            self.config,
            connect=self.connect,
            provider=self.provider,
            clock=self.clock,
            sleep=self.sleep,
            handled_by="test-poller",
            **kwargs,
        )


@pytest.fixture
def harness(conn, test_db, tmp_path):
    def make(start: datetime = at(12), **config) -> Harness:
        return Harness(test_db, tmp_path, start=start, **config)

    return make


def test_regular_interval(harness) -> None:
    h = harness(start=at(12))
    assert h.poller(max_passes=3).run(install_signals=False) == 0
    assert h.provider.times == [at(12), at(12, 20), at(12, 40)]
    # Between passes it wakes up every request_check_s to look for requests.
    assert max(h.sleeps) == h.config.request_check_s


def test_jitter_varies_the_interval(harness) -> None:
    h = harness(start=at(12), jitter_s=120)
    h.poller(max_passes=6).run(install_signals=False)
    gaps = [(b - a).total_seconds() for a, b in zip(h.provider.times, h.provider.times[1:])]
    assert all(1080 - 5 <= g <= 1320 + 5 for g in gaps)
    assert len(set(gaps)) > 1


def test_morning_window_polls_often_until_sleep_arrives(harness) -> None:
    h = harness(start=at(5))

    def watch_syncs(h):
        if h.clock.now >= at(5, 12) and DAY not in h.fake.sleep:
            h.fake.set_wellness(DAY, sleep=synthetic.sleep(DAY))

    h.hooks.append(watch_syncs)
    h.poller(max_passes=6).run(install_signals=False)
    assert h.provider.times == [
        at(5),
        at(5, 5),
        at(5, 10),
        at(5, 15),  # sleep arrives here
        at(5, 35),
        at(5, 55),
    ]


def test_morning_window_bounds() -> None:
    from datetime import time

    assert in_window(time(5), time(5), time(11)) and not in_window(time(11), time(5), time(11))
    assert in_window(time(23), time(22), time(2)) and in_window(time(1), time(22), time(2))
    assert not in_window(time(12), time(22), time(2))


def test_a_failing_pass_never_ends_the_loop(harness, conn) -> None:
    h = harness()
    h.fake.fail("get_activities_by_date", GarminError("HTTP 502"), RuntimeError("bug"))
    poller = h.poller(max_passes=3)
    assert poller.run(install_signals=False) == 0
    assert len(h.provider.times) == 3 and poller.last_outcome.ok
    status = state.get(conn, state.STATUS)
    assert status["consecutive_failures"] == 0 and status["last_error"] == "RuntimeError: bug"


def test_rate_limit_moves_the_next_pass(harness, conn) -> None:
    h = harness()
    h.fake.fail("get_activities_by_date", GarminRateLimited("429"))
    h.poller(max_passes=2).run(install_signals=False)
    assert h.provider.times == [at(12), at(12, 20)]  # 5 min back-off < interval
    h = harness(rate_limit_backoff_s=3600)
    state.put(conn, state.RATE_LIMIT, {})
    h.fake.fail("get_activities_by_date", GarminRateLimited("429"))
    h.poller(max_passes=2).run(install_signals=False)
    assert h.provider.times == [at(12), at(13)]


def test_lost_database_connection_is_retried(harness) -> None:
    h = harness()
    real_connect = h.connect
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise psycopg.OperationalError("connection refused")
        return real_connect()

    h.connect = flaky
    h.poller(max_passes=1).run(install_signals=False)
    assert len(attempts) == 2 and h.sleeps[0] == h.config.db_retry_s
    assert h.provider.times == [at(12) + timedelta(seconds=h.config.db_retry_s)]


# Advisory lock ----------------------------------------------------------------------------------


def test_second_poller_stands_by_until_the_lock_is_free(harness, test_db) -> None:
    h = harness()
    holder = db.connect(test_db.app_url)
    holder.execute("SELECT pg_advisory_lock(%s)", (POLLER_LOCK,))
    assert lock_held(holder)

    def release_after_two_waits(h):
        if len(h.sleeps) == 2 and not holder.closed:
            holder.execute("SELECT pg_advisory_unlock(%s)", (POLLER_LOCK,))
            holder.close()

    h.hooks.append(release_after_two_waits)
    try:
        h.poller(max_passes=1).run(install_signals=False)
    finally:
        holder.close()
    assert h.sleeps[:2] == [h.config.lock_retry_s] * 2
    assert h.provider.times == [at(12) + timedelta(seconds=2 * h.config.lock_retry_s)]


def test_lock_is_held_while_running_and_released_after(harness, conn) -> None:
    h = harness()
    seen = []
    h.hooks.append(lambda h: seen.append(lock_held(conn)))
    h.poller(max_passes=2).run(install_signals=False)
    assert seen and all(seen)
    assert not lock_held(conn)


# SIGTERM ----------------------------------------------------------------------------------------


def test_sigterm_between_passes_exits_cleanly(harness) -> None:
    h = harness()
    previous = signal.getsignal(signal.SIGTERM)
    h.hooks.append(lambda h: len(h.sleeps) == 3 and os.kill(os.getpid(), signal.SIGTERM))
    poller = h.poller()
    assert poller.run() == 0
    assert poller.stopping and len(h.provider.times) == 1
    assert signal.getsignal(signal.SIGTERM) is previous


def test_sigterm_during_a_pass_finishes_the_step_then_stops(harness, conn) -> None:
    h = harness()
    h.fake.set_wellness(DAY, hrv=synthetic.hrv(DAY), stats=synthetic.stats(DAY))

    def term_while_fetching_sleep():
        os.kill(os.getpid(), signal.SIGTERM)
        return synthetic.sleep(DAY)

    h.fake.fail("get_sleep_data", term_while_fetching_sleep)
    poller = h.poller()
    assert poller.run() == 0
    result = poller.last_outcome.result
    assert result.stopped_early and result.wellness_days == [DAY]  # today's step completed
    assert h.fake.days_requested("get_sleep_data") == [DAY]  # no further day started
    assert h.fake.call_count("get_personal_records") == 0
    assert "last_success_at" not in state.get(conn, state.STATUS)
    row = conn.execute("SELECT sleep_s, steps FROM wellness_daily WHERE wake_date = %s", (DAY,))
    assert row.fetchone()["steps"] == 8000


# sync_now requests -----------------------------------------------------------------------------


def test_requests_are_served_between_passes(harness, conn, agent_conn) -> None:
    h = harness()
    ids = []

    def athlete_asks(h):
        if h.clock.now >= at(12, 3) and not ids:
            h.fake.add_activity(activity_at(7001, at(11, 30)))
            ids.append(agent_requests.create_request(agent_conn))  # the agent's role

    h.hooks.append(athlete_asks)
    h.poller(max_passes=3).run(install_signals=False)
    asked_at = at(12, 3)
    assert h.provider.times[0] == at(12)
    assert asked_at <= h.provider.times[1] <= asked_at + timedelta(seconds=5)
    assert h.provider.times[2] == h.provider.times[1] + timedelta(minutes=20)
    row = agent_requests.get_request(conn, ids[0])
    assert row["status"] == "done" and row["handled_by"] == "test-poller"
    assert row["result"] == {
        "new_activities": [7001],
        "sleep_synced": False,
        "wake_date": "2030-01-02",
    }
    assert row["started_at"] is not None and row["finished_at"] is not None


def test_requests_during_a_rate_limit_fail_without_calling_garmin(harness, conn) -> None:
    h = harness()
    state.note_rate_limit(conn, at(12), base_s=3600, max_s=3600)
    request_id = agent_requests.create_request(conn)
    poller = h.poller(max_passes=1)
    poller.run(install_signals=False)
    row = agent_requests.get_request(conn, request_id)
    assert row["status"] == "failed" and "rate-limiting" in row["error"]
    assert h.fake.calls == [] and h.provider.times == []


def test_failed_pass_fails_the_request_and_orphans_are_cleaned(harness, conn) -> None:
    orphan = agent_requests.create_request(conn)
    agent_requests.claim_pending(conn, "dead-process")
    h = harness()
    h.fake.fail("get_activities_by_date", GarminError("HTTP 502"))
    request_id = agent_requests.create_request(conn)
    h.poller(max_passes=1).run(install_signals=False)
    failed = agent_requests.get_request(conn, request_id)
    assert failed["status"] == "failed" and "HTTP 502" in failed["error"]
    leftover = agent_requests.get_request(conn, orphan)
    assert leftover["status"] == "failed" and "interrupted" in leftover["error"]
