"""History import: chunks, FIT files, wellness, politeness, resuming, no events."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests import synthetic
from tests.sync.helpers import activity_at, fit_file, make_config, zipped
from thresy.garmin.errors import GarminError, GarminRateLimited
from thresy.garmin.fake import FakeGarminClient
from thresy.sync import state
from thresy.sync.backfill import BackfillStopped, backfill, clamp_until

pytestmark = pytest.mark.db

NOW = datetime(2030, 3, 1, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
SINCE = date(2030, 1, 1)


def history(days: int = 59) -> FakeGarminClient:
    """One run every 10 days and full wellness for every day from SINCE."""
    fake = FakeGarminClient()
    for i in range(days + 1):
        day = SINCE + timedelta(days=i)
        fake.set_wellness(day, sleep=synthetic.sleep(day), hrv=synthetic.hrv(day),
                          stats=synthetic.stats(day))  # fmt: skip
        if i % 10 == 0:
            start = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
            fake.add_activity(activity_at(8000 + i, start), original=zipped(fit_file(start)))
    return fake


def count(conn, sql: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {sql}").fetchone()["n"]


def test_clamp_until() -> None:
    assert clamp_until(None, TODAY) == TODAY - timedelta(days=1)
    assert clamp_until(TODAY, TODAY) == TODAY - timedelta(days=1)
    assert clamp_until(SINCE, TODAY) == SINCE


def test_full_backfill_imports_everything_politely_without_events(conn, tmp_path) -> None:
    config = make_config(tmp_path, backfill_pause_s=1.5)
    fake = history()
    sleeps: list[float] = []
    result = backfill(conn, fake, config, since=SINCE, until=TODAY, now=NOW, sleep=sleeps.append)

    assert result.finished and result.until == TODAY - timedelta(days=1)
    assert result.activities == result.new_activities == result.fit_stored == 6
    assert result.wellness_days == (result.until - SINCE).days + 1
    assert count(conn, "activities WHERE has_records") == 6
    assert (config.data_dir / "fit/2030/8000.fit").is_file()
    assert count(conn, "wellness_daily WHERE sleep_synced_at IS NOT NULL") == 59
    assert count(conn, "events") == 0
    assert state.get(conn, state.PENDING) == {}
    # A pause before every request, and no request for today.
    assert sleeps == [1.5] * result.requests == [1.5] * len(fake.calls)
    assert TODAY not in fake.days_requested("get_sleep_data")
    # Activity lists in chunks of four weeks.
    ranges = [arg for name, arg in fake.calls if name == "get_activities_by_date"]
    assert ranges[0] == (SINCE, SINCE + timedelta(days=27)) and len(ranges) == 3
    assert state.get(conn, state.BACKFILL)["finished_at"] is not None


def test_interrupted_backfill_resumes_where_it_stopped(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    fake = history()
    calls = []

    def stop_after_twenty_requests():
        calls.append(1)
        return len(fake.calls) >= 20

    first = backfill(conn, fake, config, since=SINCE, now=NOW, sleep=lambda _s: None,
                     should_stop=stop_after_twenty_requests)  # fmt: skip
    assert first.stopped_early and not first.finished
    done_days = state.get(conn, state.BACKFILL).get("wellness_done_through")
    requested = len(fake.calls)

    fake.calls.clear()
    second = backfill(conn, fake, config, since=SINCE, now=NOW, sleep=lambda _s: None)
    assert second.resumed and second.finished
    # Nothing done before is requested again.
    assert fake.call_count("get_activities_by_date") == 0
    assert fake.call_count("download_original") == 0
    if done_days:
        assert date.fromisoformat(done_days) not in fake.days_requested("get_sleep_data")
    # Together exactly the requests of one uninterrupted run: 3 activity lists, 6 downloads,
    # sleep, HRV and stats for 59 days.
    assert requested + len(fake.calls) == 3 + 6 + 59 * 3
    assert count(conn, "wellness_daily") == 59 and count(conn, "activities WHERE has_records") == 6


def test_rerun_skips_complete_days_and_existing_files(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    fake = history()
    backfill(conn, fake, config, since=SINCE, now=NOW, sleep=lambda _s: None)
    fake.calls.clear()
    again = backfill(conn, fake, config, since=SINCE, now=NOW, sleep=lambda _s: None)
    assert not again.resumed and again.finished  # a finished job starts over
    assert fake.call_count("download_original") == 0
    # Only the days whose step count the poller does not consider final yet are fetched again.
    assert again.wellness_skipped >= 59 - 2
    assert fake.call_count("get_sleep_data") == 0


def test_rate_limits_back_off_and_eventually_stop(conn, tmp_path) -> None:
    config = make_config(tmp_path, rate_limit_backoff_s=60, backfill_max_rate_limits=3)
    fake = history(days=3)
    fake.fail("get_activities_by_date", GarminRateLimited("429"), GarminRateLimited("429"))
    sleeps: list[float] = []
    result = backfill(conn, fake, config, since=SINCE, until=SINCE + timedelta(days=3), now=NOW,
                      sleep=sleeps.append)  # fmt: skip
    assert result.finished and sleeps[:2] == [60, 120]

    fake.fail("get_activities_by_date", *[GarminRateLimited("429")] * 3)
    with pytest.raises(BackfillStopped, match="run the same command later"):
        backfill(conn, fake, config, since=SINCE, until=SINCE + timedelta(days=3), now=NOW,
                 sleep=sleeps.append, restart=True)  # fmt: skip
    job = state.get(conn, state.BACKFILL)
    assert not job.get("finished_at") and not job.get("activities_done_through")
    resumed = backfill(conn, fake, config, since=SINCE, until=SINCE + timedelta(days=3),
                       now=NOW, sleep=sleeps.append)  # fmt: skip
    assert resumed.resumed and resumed.finished


def test_a_bad_day_is_skipped_with_a_warning(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    fake = history(days=2)
    fake.fail("get_sleep_data", GarminError("no data received"))
    result = backfill(conn, fake, config, since=SINCE, until=SINCE + timedelta(days=2), now=NOW,
                      sleep=lambda _s: None)  # fmt: skip
    assert result.finished and len(result.warnings) == 1
    assert result.wellness_days == 2


def test_only_activities_or_only_wellness(conn, tmp_path) -> None:
    config = make_config(tmp_path)
    fake = history(days=12)
    end = SINCE + timedelta(days=12)
    result = backfill(conn, fake, config, since=SINCE, until=end, now=NOW, wellness=False,
                      fit=False, sleep=lambda _s: None)  # fmt: skip
    assert result.activities == 2 and result.fit_stored == 0 and result.wellness_days == 0
    assert fake.call_count("download_original") == fake.call_count("get_sleep_data") == 0
    assert count(conn, "activities WHERE fit_path IS NULL") == 2


def test_nothing_to_do(conn, tmp_path) -> None:
    fake = FakeGarminClient()
    result = backfill(conn, fake, make_config(tmp_path), since=TODAY, now=NOW)
    assert result.finished and fake.calls == []
