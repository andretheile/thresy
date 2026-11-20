"""One sync pass against the fake client: wellness, activities with FIT files, events."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests import synthetic
from tests.sync.helpers import (
    Clock,
    StaticProvider,
    activity_at,
    fit_file,
    make_config,
    zipped,
)
from thresy.garmin.errors import GarminAuthRequired, GarminError, GarminRateLimited
from thresy.garmin.fake import FakeGarminClient
from thresy.sync import state
from thresy.sync.passes import run_pass, sync_once

pytestmark = pytest.mark.db

TODAY = date(2030, 1, 2)
YESTERDAY = TODAY - timedelta(days=1)


def at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def events(conn, kind: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT kind, dedupe_key, payload FROM events ORDER BY id").fetchall()
    return [r for r in rows if kind is None or r["kind"] == kind]


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def fake() -> FakeGarminClient:
    return FakeGarminClient()


# Sleep ------------------------------------------------------------------------------------------


def test_sleep_arrives_once_for_today_and_never_for_yesterday(conn, config, fake) -> None:
    # Yesterday's night is already on Garmin; today's is still the pre-sync placeholder.
    fake.set_wellness(YESTERDAY, sleep=synthetic.sleep(YESTERDAY), hrv=synthetic.hrv(YESTERDAY))
    fake.set_wellness(TODAY, sleep=synthetic.sleep(TODAY, synced=False))

    first = run_pass(conn, fake, config, now=at(5))
    assert first.wake_date == TODAY and not first.sleep_synced and not first.sleep_arrived
    assert events(conn, "sleep_arrived") == []
    row = conn.execute("SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %s",
                       (YESTERDAY,)).fetchone()  # fmt: skip
    assert row["sleep_synced_at"] is not None  # stored, but no event for yesterday

    assert not run_pass(conn, fake, config, now=at(5, 20)).sleep_synced
    assert events(conn, "sleep_arrived") == []

    fake.set_wellness(TODAY, sleep=synthetic.sleep(TODAY), hrv=synthetic.hrv(TODAY))
    arrived = run_pass(conn, fake, config, now=at(5, 40))
    assert arrived.sleep_arrived and arrived.sleep_synced
    assert arrived.events == [f"sleep:{TODAY.isoformat()}"]
    later = run_pass(conn, fake, config, now=at(6))
    assert later.sleep_synced and not later.sleep_arrived and later.events == []

    assert events(conn, "sleep_arrived") == [
        {
            "kind": "sleep_arrived",
            "dedupe_key": f"sleep:{TODAY.isoformat()}",
            "payload": {"wake_date": TODAY.isoformat()},
        }
    ]
    synced = conn.execute("SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %s",
                          (TODAY,)).fetchone()["sleep_synced_at"]  # fmt: skip
    assert synced == at(5, 40)


def test_next_morning_raises_the_next_night_and_skips_finished_days(conn, config, fake) -> None:
    for day in (YESTERDAY, TODAY):
        fake.set_wellness(day, sleep=synthetic.sleep(day), hrv=synthetic.hrv(day),
                          stats=synthetic.stats(day))  # fmt: skip
    run_pass(conn, fake, config, now=at(13))
    tomorrow = TODAY + timedelta(days=1)
    fake.set_wellness(tomorrow, sleep=synthetic.sleep(tomorrow))
    fake.calls.clear()
    result = run_pass(conn, fake, config, now=at(7, day=tomorrow))
    assert result.events == [f"sleep:{tomorrow.isoformat()}"]
    # Yesterday (now TODAY) has sleep and HRV: only its unfinished step count is fetched.
    assert fake.days_requested("get_sleep_data")[0] == tomorrow
    assert TODAY not in fake.days_requested("get_sleep_data")
    assert TODAY not in fake.days_requested("get_hrv_data")
    assert TODAY in fake.days_requested("get_stats")
    keys = [e["dedupe_key"] for e in events(conn, "sleep_arrived")]
    assert keys == [f"sleep:{TODAY.isoformat()}", f"sleep:{tomorrow.isoformat()}"]


def test_incomplete_days_are_fetched_again(conn, config, fake) -> None:
    fake.set_wellness(YESTERDAY, sleep=synthetic.sleep(YESTERDAY),
                      stats=synthetic.stats(YESTERDAY, steps=9000))  # fmt: skip
    run_pass(conn, fake, config, now=at(6))
    assert YESTERDAY in fake.days_requested("get_hrv_data")

    # Night with sleep but no HRV yet, step count fetched before noon: both again.
    fake.calls.clear()
    fake.set_wellness(YESTERDAY, hrv=synthetic.hrv(YESTERDAY),
                      stats=synthetic.stats(YESTERDAY, steps=11000))  # fmt: skip
    run_pass(conn, fake, config, now=at(9))
    assert YESTERDAY in fake.days_requested("get_hrv_data")
    assert YESTERDAY in fake.days_requested("get_stats")
    assert YESTERDAY not in fake.days_requested("get_sleep_data")

    # After noon the count is final; afterwards yesterday needs nothing more.
    run_pass(conn, fake, config, now=at(13))
    fake.calls.clear()
    run_pass(conn, fake, config, now=at(14))
    assert YESTERDAY not in [d for _, d in fake.calls]
    row = conn.execute("SELECT steps, hrv_night_avg FROM wellness_daily WHERE wake_date = %s",
                       (YESTERDAY,)).fetchone()  # fmt: skip
    assert row == {"steps": 11000, "hrv_night_avg": 60.0}


def test_older_days_are_rechecked_at_most_every_interval(conn, config, fake) -> None:
    older = TODAY - timedelta(days=3)
    run_pass(conn, fake, config, now=at(6))
    assert older in fake.days_requested("get_sleep_data")
    fake.calls.clear()
    run_pass(conn, fake, config, now=at(7))
    assert older not in fake.days_requested("get_sleep_data")
    assert YESTERDAY in fake.days_requested("get_sleep_data")  # yesterday: every pass
    fake.calls.clear()
    run_pass(conn, fake, config, now=at(9, 1))
    assert older in fake.days_requested("get_sleep_data")
    too_old = TODAY - timedelta(days=2 + config.recheck_days)
    assert too_old not in fake.days_requested("get_sleep_data")


def test_partial_steps_never_decrease_and_empty_payloads_do_not_overwrite(conn, config, fake):
    fake.set_wellness(TODAY, hrv=synthetic.hrv(TODAY), stats=synthetic.stats(TODAY, steps=5000))
    run_pass(conn, fake, config, now=at(12))
    fake.hrv[TODAY] = {}  # Garmin's 204
    fake.stats[TODAY] = synthetic.stats(TODAY, steps=4000)
    run_pass(conn, fake, config, now=at(13))
    row = conn.execute("SELECT steps, raw_hrv IS NOT NULL AS has_hrv FROM wellness_daily"
                       " WHERE wake_date = %s", (TODAY,)).fetchone()  # fmt: skip
    assert row == {"steps": 5000, "has_hrv": True}


def test_steps_intervals_only_when_stats_have_no_count(conn, config, fake) -> None:
    fake.set_wellness(TODAY, stats=synthetic.stats(TODAY, steps=None),
                      steps=[{"steps": 100}, {"steps": 250}])  # fmt: skip
    run_pass(conn, fake, config, now=at(12))
    assert TODAY in fake.days_requested("get_steps_data")
    steps = conn.execute("SELECT steps FROM wellness_daily WHERE wake_date = %s",
                         (TODAY,)).fetchone()["steps"]  # fmt: skip
    assert steps == 350
    fake.calls.clear()
    fake.stats[TODAY] = synthetic.stats(TODAY, steps=400)
    run_pass(conn, fake, config, now=at(13))
    assert TODAY not in fake.days_requested("get_steps_data")


# Activities -------------------------------------------------------------------------------------


def test_new_activity_end_to_end(conn, config, fake) -> None:
    start = at(5)
    fake.add_activity(activity_at(5001, start), original=zipped(fit_file(start, seconds=30)))
    result = run_pass(conn, fake, config, now=at(6, 30))
    assert result.new_activities == [5001] and result.fit_stored == [5001]
    assert result.events == ["activity:5001"]

    path = config.data_dir / "fit" / "2030" / "5001.fit"
    assert path.read_bytes() == fit_file(start, seconds=30)
    row = conn.execute("SELECT has_records, fit_path, source FROM activities WHERE id = 5001")
    assert row.fetchone() == {"has_records": True, "fit_path": "fit/2030/5001.fit",
                              "source": "garmin"}  # fmt: skip
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM activity_records WHERE activity_id = 5001) AS records,"
        " (SELECT count(*) FROM activity_laps WHERE activity_id = 5001) AS laps"
    ).fetchone()
    assert counts == {"records": 30, "laps": 1}
    assert events(conn, "new_activity") == [
        {
            "kind": "new_activity",
            "dedupe_key": "activity:5001",
            "payload": {"activity_id": 5001, "type": "running", "is_run": True},
        }
    ]

    fake.calls.clear()
    again = run_pass(conn, fake, config, now=at(7))
    assert again.new_activities == [] and again.events == []
    assert fake.call_count("download_original") == 0
    assert len(events(conn, "new_activity")) == 1
    assert state.get(conn, state.PENDING) == {}


def test_old_activities_raise_no_event_but_get_their_file(conn, config, fake) -> None:
    old_start = at(7, day=TODAY - timedelta(days=4))
    fake.add_activity(activity_at(5002, old_start), original=fit_file(old_start))  # bare FIT
    result = run_pass(conn, fake, config, now=at(6))
    assert result.new_activities == [5002] and result.fit_stored == [5002]
    assert result.events == [] and events(conn, "new_activity") == []
    assert state.get(conn, state.PENDING) == {}


def test_first_sync_of_a_long_history_raises_events_only_for_recent_ones(conn, config, fake):
    for i in range(6):
        start = at(6) - timedelta(days=i, hours=1)
        fake.add_activity(activity_at(6000 + i, start, type_key="walking" if i else "running"))
    result = run_pass(conn, fake, config, now=at(6))
    assert len(result.new_activities) == 6
    # Started 1 h, 25 h and 49 h ago: the first two are within 48 h.
    assert sorted(e["payload"]["activity_id"] for e in events(conn, "new_activity")) == [
        6000,
        6001,
    ]
    walk = next(e for e in events(conn, "new_activity") if e["payload"]["activity_id"] == 6001)
    assert walk["payload"] == {"activity_id": 6001, "type": "walking", "is_run": False}


def test_event_waits_for_a_failed_download_and_is_raised_when_it_works(conn, config, fake):
    start = at(5)
    fake.add_activity(activity_at(5003, start), original=zipped(fit_file(start)))
    fake.fail("download_original", GarminError("HTTP 503"))
    first = run_pass(conn, fake, config, now=at(6))
    assert first.fit_failed == {5003: "download failed: HTTP 503"}
    assert first.events == [] and set(state.get(conn, state.PENDING)) == {"5003"}
    second = run_pass(conn, fake, config, now=at(6, 20))
    assert second.fit_stored == [5003] and second.events == ["activity:5003"]
    assert state.get(conn, state.FIT_FAILURES) == {}


def test_download_gives_up_after_max_attempts_and_raises_the_event(conn, config, fake) -> None:
    start = at(5)
    fake.add_activity(activity_at(5004, start), original=zipped(fit_file(start)))
    fake.fail("download_original", *[GarminError("HTTP 503")] * config.fit_max_attempts)
    for minute in range(config.fit_max_attempts):
        result = run_pass(conn, fake, config, now=at(6, minute))
    assert result.events == ["activity:5004"]
    fake.calls.clear()
    run_pass(conn, fake, config, now=at(7))
    assert fake.call_count("download_original") == 0  # given up
    failure = state.get(conn, state.FIT_FAILURES)["5004"]
    assert failure["attempts"] == config.fit_max_attempts and not failure["permanent"]


def test_activity_without_file_and_broken_files(conn, config, fake) -> None:
    fake.add_activity(activity_at(5005, at(5)))  # manual activity: 404
    fake.add_activity(activity_at(5006, at(4)), original=b"not a fit file at all")
    fake.add_activity(activity_at(5007, at(3)), original=zipped(b"x" * 20)[:30])
    result = run_pass(conn, fake, config, now=at(6))
    assert set(result.fit_failed) == {5005, 5006, 5007}
    assert sorted(result.events) == ["activity:5005", "activity:5006", "activity:5007"]
    failures = state.get(conn, state.FIT_FAILURES)
    assert all(failures[k]["permanent"] for k in ("5005", "5006", "5007"))
    fake.calls.clear()
    run_pass(conn, fake, config, now=at(7))
    assert fake.call_count("download_original") == 0


def test_unparsable_fit_is_kept_with_its_path(conn, config, fake) -> None:
    garbage_fit = b"\x0e\x20\x00\x00\x10\x00\x00\x00.FIT\x00\x00" + b"\x00" * 16
    fake.add_activity(activity_at(5008, at(5)), original=garbage_fit)
    result = run_pass(conn, fake, config, now=at(6))
    assert 5008 in result.fit_failed
    row = conn.execute("SELECT has_records, fit_path FROM activities WHERE id = 5008").fetchone()
    assert row == {"has_records": False, "fit_path": "fit/2030/5008.fit"}
    assert (config.data_dir / "fit/2030/5008.fit").is_file()


def test_no_events_without_raise_events(conn, config, fake) -> None:
    fake.add_activity(activity_at(5009, at(5)), original=zipped(fit_file(at(5))))
    fake.set_wellness(TODAY, sleep=synthetic.sleep(TODAY))
    result = run_pass(conn, fake, config, now=at(6), raise_events=False)
    assert result.sleep_arrived and result.new_activities == [5009]
    assert events(conn) == [] and state.get(conn, state.PENDING) == {}


def test_should_stop_ends_the_pass_between_steps(conn, config, fake) -> None:
    fake.add_activity(activity_at(5010, at(5)), original=zipped(fit_file(at(5))))
    result = run_pass(conn, fake, config, now=at(6), should_stop=lambda: True)
    assert result.stopped_early and result.new_activities == [5010]
    assert fake.call_count("download_original") == 0 and fake.call_count("get_sleep_data") == 0


# Personal records -------------------------------------------------------------------------------


def test_personal_records_refresh_daily_and_after_new_runs(conn, config, fake) -> None:
    fake.personal_records = [{"typeId": 3, "value": 1500.0, "activityId": 1}]
    assert run_pass(conn, fake, config, now=at(6)).personal_records == 1
    assert run_pass(conn, fake, config, now=at(7)).personal_records is None
    fake.add_activity(activity_at(5011, at(7)), original=zipped(fit_file(at(7))))
    assert run_pass(conn, fake, config, now=at(8)).personal_records == 1
    assert run_pass(conn, fake, config, now=at(9)).personal_records is None
    assert run_pass(conn, fake, config, now=at(8, day=TODAY + timedelta(days=1))).personal_records
    fake.fail("get_personal_records", GarminError("HTTP 500"))
    result = run_pass(conn, fake, config, now=at(9, day=TODAY + timedelta(days=2)))
    assert result.personal_records is None and "personal records" in result.warnings[0]
    label = conn.execute("SELECT label FROM personal_records").fetchone()["label"]
    assert label == "5k"


# sync_once: bookkeeping, back-off, sync_failed ---------------------------------------------------


def test_sync_once_records_success(conn, config, fake) -> None:
    outcome = sync_once(conn, config, StaticProvider(fake), now=at(6))
    assert outcome.ok
    status = state.get(conn, state.STATUS)
    assert status["last_success_at"] == at(6).isoformat()
    assert status["consecutive_failures"] == 0
    assert status["last_result"] == {"new_activities": [], "sleep_synced": False,
                                     "wake_date": TODAY.isoformat()}  # fmt: skip


def test_rate_limit_backs_off_exponentially(conn, config, fake) -> None:
    clock = Clock(at(6))
    provider = StaticProvider(fake)
    fake.fail("get_activities_by_date", GarminRateLimited("429"), GarminRateLimited("429"))
    first = sync_once(conn, config, provider, now=clock())
    assert first.error_kind == "rate_limit" and not first.skipped
    until = state.rate_limited_until(conn)
    assert until == clock() + timedelta(seconds=config.rate_limit_backoff_s)

    clock.advance(60)
    calls = len(fake.calls)
    skipped = sync_once(conn, config, provider, now=clock())
    assert skipped.skipped and len(fake.calls) == calls  # no request during the back-off

    clock.now = until
    second = sync_once(conn, config, provider, now=clock())
    assert second.error_kind == "rate_limit"
    assert state.rate_limited_until(conn) == until + timedelta(
        seconds=2 * config.rate_limit_backoff_s
    )
    clock.now = state.rate_limited_until(conn)
    assert sync_once(conn, config, provider, now=clock()).ok
    assert state.rate_limited_until(conn) is None
    assert state.get(conn, state.RATE_LIMIT) == {}


def test_sync_failed_after_repeated_failures_once_per_hour(conn, config, fake) -> None:
    provider = StaticProvider(fake)
    fake.fail("get_activities_by_date", *[GarminError("HTTP 502")] * 5)
    outcomes = [sync_once(conn, config, provider, now=at(6, m * 10)) for m in range(5)]
    assert [o.error_kind for o in outcomes] == ["garmin"] * 5
    failed = events(conn, "sync_failed")
    assert len(failed) == 1 and failed[0]["dedupe_key"] == "sync_failed:2030-01-02:06"
    assert "HTTP 502" in failed[0]["payload"]["error"]
    assert state.get(conn, state.STATUS)["consecutive_failures"] == 5
    assert sync_once(conn, config, provider, now=at(7)).ok
    assert state.get(conn, state.STATUS)["consecutive_failures"] == 0


def test_bugs_in_a_pass_are_failures_not_crashes(conn, config, fake) -> None:
    fake.fail("get_activities_by_date", lambda: [{"activityId": None}], KeyError("boom"))
    first = sync_once(conn, config, StaticProvider(fake), now=at(6))
    assert first.ok and first.result.warnings  # a broken summary is skipped
    second = sync_once(conn, config, StaticProvider(fake), now=at(7))
    assert second.error_kind == "error" and "KeyError" in second.error


def test_stored_errors_are_redacted(conn, config, fake) -> None:
    """The error of a failed pass is stored, raised as ``sync_failed`` and shown to the
    athlete; an unexpected exception can quote a URL with credentials."""
    leak = RuntimeError("proxy http://user:fake-proxy-pw-0000@proxy.invalid:3128 refused")
    fake.fail("get_activities_by_date", *[leak] * 5)
    outcomes = [sync_once(conn, config, StaticProvider(fake), now=at(6, m)) for m in range(5)]
    assert outcomes[-1].error_kind == "error"
    stored = [
        outcomes[-1].error,
        state.get(conn, state.STATUS)["last_error"],
        events(conn, "sync_failed")[0]["payload"]["error"],
    ]
    for text in stored:
        assert "fake-proxy-pw-0000" not in text
        assert "http://user:<redacted>@proxy.invalid" in text


def test_auth_problems_have_their_own_event(conn, config, fake) -> None:
    provider = StaticProvider(fake)
    provider.error = GarminAuthRequired("no_credentials", "log in please")
    for minute in range(5):
        outcome = sync_once(conn, config, provider, now=at(6, minute))
    assert outcome.error_kind == "auth" and outcome.error == "log in please"
    assert provider.invalidated == 5
    assert events(conn, "sync_failed") == []
