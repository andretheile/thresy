"""Night, wellness trend and weekly volume (database)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests import synthetic
from tests.analysis.synth import T0, Phase, put_activity, records
from thresy import store
from thresy.analysis import night as nt
from thresy.analysis import weeks as wk
from thresy.analysis import wellness as wl

pytestmark = pytest.mark.db

D = date(2030, 3, 4)
UTC_ZONE = ZoneInfo("UTC")


def _night(conn) -> None:
    # sleep from 22:00 UTC: light 60 min, awake 30, REM 60, deep 60, light 60
    sleep = synthetic.sleep(D, levels=[(1, 60), (3, 30), (2, 60), (0, 60), (1, 60)])
    # readings every 5 min from 23:00: 50 ms while awake (23:00-23:30), 80 ms otherwise
    hrv = synthetic.hrv(D, values=[50] * 6 + [80] * 30, night_avg=75)
    store.upsert_wellness(conn, D, sleep=sleep, hrv=hrv, stats=synthetic.stats(D))


def test_night_uses_the_correct_stage_mapping(conn) -> None:
    _night(conn)
    r = nt.night(conn, D, UTC_ZONE)
    assert r["synced"] is True
    h = r["hrv"]
    assert h["n"] == 36 and h["n_awake"] == 6
    assert h["avg_all"] == pytest.approx((6 * 50 + 30 * 80) / 36, abs=0.05)
    assert h["avg_asleep"] == 80.0  # REM (level 2) counts as asleep, not awake
    assert h["avg_first_3h"] == 80.0
    assert h["n_first_3h"] == 18  # 23:30 to 00:55, the awake readings left out
    assert h["per_stage"] == {"deep": 80.0, "light": 80.0, "rem": 80.0, "awake": 50.0}
    assert h["min"]["value_ms"] == 50 and h["min"]["local"] == datetime(2030, 3, 3, 23, 0)
    (awake,) = r["awake_periods"]
    assert (awake["start_local"], awake["duration_s"]) == (datetime(2030, 3, 3, 23, 0), 1800)
    assert r["sleep"]["stages_from_levels_s"] == {"light": 7200, "awake": 1800, "rem": 3600,
                                                  "deep": 3600}  # fmt: skip
    assert r["garmin_hrv"]["night_avg"] == 75


def test_night_local_times_follow_the_payload_offset(conn) -> None:
    sleep = synthetic.sleep(D, levels=[(1, 60), (3, 30)])
    dto = sleep["dailySleepDTO"]
    dto["sleepStartTimestampLocal"] = dto["sleepStartTimestampGMT"] + 2 * 3600 * 1000
    store.upsert_wellness(conn, D, sleep=sleep)
    r = nt.night(conn, D, UTC_ZONE)
    assert r["sleep"]["start_local"] == datetime(2030, 3, 4, 0, 0)  # 22:00 UTC + 2 h
    assert r["awake_periods"][0]["start_local"] == datetime(2030, 3, 4, 1, 0)


def test_night_not_synced_yet(conn) -> None:
    _night(conn)
    tomorrow = D + timedelta(days=1)
    store.upsert_wellness(conn, tomorrow, sleep=synthetic.sleep(tomorrow, synced=False))
    r = nt.night(conn, tomorrow, UTC_ZONE)
    assert r["synced"] is False and r["latest_synced"] == D


def test_wellness_trend(conn) -> None:
    for n in range(14):
        day = D - timedelta(days=13 - n)
        hrv = synthetic.hrv(day, night_avg=45 if n < 7 else 60)  # balanced range 50-70
        store.upsert_wellness(
            conn, day, sleep=synthetic.sleep(day), hrv=hrv,
            stats=synthetic.stats(day, resting_hr=50 + (n >= 7)),
        )  # fmt: skip
    r = wl.trend(conn, D, days=14)
    s = r["summary"]
    assert s["last7"]["hrv"] == 60 and s["prev7"]["hrv"] == 45
    assert s["last7"]["resting_hr"] == 51 and s["prev7"]["resting_hr"] == 50
    assert s["nights_below_range"] == 7 and s["nights_above_range"] == 0
    assert len(r["daily"]) == 14 and r["daily"][0]["below_range"] is True
    assert s["days_without_sleep"] == 0
    gap = wl.trend(conn, D + timedelta(days=2), days=3)
    assert gap["summary"]["days_without_sleep"] == 2


def test_weeks_and_year_to_date(conn) -> None:
    monday = date(2030, 3, 4)  # a Monday
    for k, day in enumerate([monday, monday + timedelta(days=2), monday - timedelta(days=7)]):
        t0 = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=7)
        put_activity(conn, 500 + k, records([Phase(1000 + 500 * k, speed=3.0)], t0=t0), t0=t0)
    put_activity(conn, 510, records([Phase(600, speed=6.0)], t0=T0), t0=T0, type_key="cycling")
    r = wk.weeks(conn, monday + timedelta(days=3), n_weeks=3)
    this, last = r["weeks"][-1], r["weeks"][-2]
    assert this["runs"] == 2 and this["distance_km"] == pytest.approx(7.5, abs=0.01)
    assert this["longest_km"] == pytest.approx(4.5, abs=0.01)
    assert last["runs"] == 1 and r["weeks"][0]["runs"] == 0
    assert r["ytd"]["runs"] == 3 and r["ytd_last_year"]["runs"] == 0
    with_all = wk.weeks(conn, monday + timedelta(days=3), n_weeks=1, runs_only=False)
    assert with_all["weeks"][-1]["runs"] == 3
