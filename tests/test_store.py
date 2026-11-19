from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests import synthetic
from thresy import store
from thresy.fit import Lap, Record

D = date(2030, 1, 2)


# Pure mapping ---------------------------------------------------------------------------------


def test_activity_row_maps_summary_fields() -> None:
    row = store.activity_row(synthetic.activity(1001))
    assert row["id"] == 1001
    assert row["start_utc"] == datetime(2030, 1, 2, 6, 0, tzinfo=UTC)
    assert row["start_local"] == datetime(2030, 1, 2, 7, 0)
    assert row["local_date"] == D
    assert row["type"] == "running" and row["is_run"] is True
    assert row["distance_m"] == 10000.0
    assert (row["duration_s"], row["moving_s"], row["elapsed_s"]) == (3000.0, 2950.0, 3060.0)
    assert row["avg_cadence_spm"] == 170.0
    assert row["location"] == "Sample Town"


def test_activity_row_fallbacks_and_errors() -> None:
    p = synthetic.activity(1, type_key="cycling", startTimeLocal=None, duration=None)
    row = store.activity_row(p)
    assert row["is_run"] is False
    assert row["duration_s"] == p["elapsedDuration"]
    assert row["local_date"] == D  # from startTimeGMT
    with pytest.raises(ValueError, match="activityId"):
        store.activity_row({"activityName": "x"})
    with pytest.raises(ValueError, match="start time"):
        store.activity_row({"activityId": 5, "activityType": {"typeKey": "running"}})


def test_parse_garmin_time_formats() -> None:
    assert store.parse_garmin_time("2030-01-02T21:13:55.0", utc=True) == datetime(
        2030, 1, 2, 21, 13, 55, tzinfo=UTC
    )
    assert store.parse_garmin_time("2030-01-02 07:00:00", utc=False) == datetime(2030, 1, 2, 7)
    assert store.parse_garmin_time(1893564000000, utc=True) == datetime(2030, 1, 2, 6, tzinfo=UTC)
    assert store.parse_garmin_time(None, utc=True) is None


def test_sleep_stage_mapping() -> None:
    assert store.SLEEP_STAGES == {0: "deep", 1: "light", 2: "rem", 3: "awake"}
    payload = synthetic.sleep(D, levels=[(0, 10), (1, 20), (2, 30), (3, 5), (1, 10)])
    rows = store.sleep_level_rows(payload, D)
    durations: dict[str, float] = {}
    for r in rows:
        durations[r["stage"]] = (
            durations.get(r["stage"], 0) + (r["end_utc"] - r["start_utc"]).total_seconds()
        )
    dto = payload["dailySleepDTO"]
    assert durations["deep"] == dto["deepSleepSeconds"]
    assert durations["light"] == dto["lightSleepSeconds"]
    assert durations["rem"] == dto["remSleepSeconds"]
    assert durations["awake"] == dto["awakeSleepSeconds"]


def test_unknown_sleep_level_is_skipped() -> None:
    payload = synthetic.sleep(D, levels=[(1, 10), (7, 10)])
    rows = store.sleep_level_rows(payload, D)
    assert [r["level_raw"] for r in rows] == [1]


def test_is_real_sleep() -> None:
    assert store.is_real_sleep(synthetic.sleep(D))
    assert not store.is_real_sleep(synthetic.sleep(D, synced=False))
    assert not store.is_real_sleep({"error": "HTTPError"})
    assert not store.is_real_sleep(None)


def test_wellness_row_rejects_payload_for_another_day() -> None:
    with pytest.raises(ValueError, match="wake date"):
        store.wellness_row(D, sleep=synthetic.sleep(D - timedelta(days=1)))
    with pytest.raises(ValueError, match="wake date"):
        store.wellness_row(D, hrv=synthetic.hrv(D + timedelta(days=1)))


def test_wellness_row_steps_fallback_and_negative_stress() -> None:
    st = synthetic.stats(D, steps=None)
    st["averageStressLevel"] = -1
    row = store.wellness_row(D, stats=st, steps=[{"steps": 100}, {"steps": 250}])
    assert row["steps"] == 350
    assert "stress_avg" not in row


def test_garmin_personal_records() -> None:
    prs = store.garmin_personal_records(
        [
            {
                "typeId": 3,
                "value": 1500.0,
                "activityId": 77,
                "activityName": "Race",
                "activityStartDateTimeLocalFormatted": "2030-01-02T09:00:00.0",
            },
            {"typeId": 99, "value": 1.0, "activityId": 0},
        ]
    )
    assert prs[0] == store.PersonalRecord(
        key="garmin_3",
        label="5k",
        value=1500.0,
        activity_id=77,
        activity_name="Race",
        date=D,
        source="garmin_pr",
    )
    assert prs[1].label == "type_99" and prs[1].activity_id is None and prs[1].date is None


# Database -------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.db


@pytestmark_db
def test_upsert_activity_is_idempotent(conn) -> None:
    payload = synthetic.activity(1001)
    assert store.upsert_activity(conn, payload, source="test") is True
    first = conn.execute("SELECT * FROM activities WHERE id = 1001").fetchone()
    assert store.upsert_activity(conn, payload, source="test") is False
    again = conn.execute("SELECT * FROM activities WHERE id = 1001").fetchone()
    assert again == first  # unchanged, updated_at did not move
    assert first["raw"]["activityName"] == "Morning Run"
    assert first["source"] == "test"
    assert conn.execute("SELECT count(*) AS n FROM activities").fetchone()["n"] == 1


@pytestmark_db
def test_upsert_activity_updates_but_keeps_records_flags(conn) -> None:
    store.upsert_activity(conn, synthetic.activity(1001), source="test")
    conn.execute("UPDATE activities SET has_records = true, fit_path = 'x.fit' WHERE id = 1001")
    store.upsert_activity(conn, synthetic.activity(1001, name="Renamed"), source="garmin")
    row = conn.execute("SELECT * FROM activities WHERE id = 1001").fetchone()
    assert row["name"] == "Renamed" and row["source"] == "garmin"
    assert row["has_records"] is True and row["fit_path"] == "x.fit"
    assert row["updated_at"] >= row["created_at"]


@pytestmark_db
def test_runs_view_pace(conn) -> None:
    store.upsert_activity(conn, synthetic.activity(1, distance=10000, moving=3000), source="t")
    store.upsert_activity(conn, synthetic.activity(2, distance=5000, moving=0), source="t")
    store.upsert_activity(conn, synthetic.activity(3, type_key="cycling"), source="t")
    store.upsert_activity(conn, synthetic.activity(4, distance=0), source="t")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM runs").fetchall()}
    assert set(rows) == {1, 2, 4}
    assert rows[1]["distance_km"] == 10 and rows[1]["pace_s_per_km"] == 300
    assert rows[1]["pace"] == "5:00/km"
    assert rows[2]["pace_s_per_km"] == 600  # moving 0 -> duration (3000 s / 5 km)
    assert rows[2]["pace"] == "10:00/km"
    assert rows[4]["pace_s_per_km"] is None and rows[4]["pace"] is None


def _records(n: int) -> list[Record]:
    t0 = datetime(2030, 1, 2, 6, tzinfo=UTC)
    return [
        Record(
            ts=t0 + timedelta(seconds=i),
            elapsed_s=float(i),
            lat=10.0 + i * 1e-5,
            lon=20.0,
            alt_m=100.0,
            dist_m=3.0 * i,
            speed_mps=3.0,
            hr=120,
            cadence_spm=170.0,
            power_w=None,
            resp_rate=None,
            temp_c=None,
        )
        for i in range(n)
    ]


def _laps(n: int) -> list[Lap]:
    return [
        Lap(i + 1, None, 60.0, 60.0, 180.0, 3.0, 3.5, 120.0, 130.0, 170.0, None,
            1.0, 1.0, None, "active", "distance", None)
        for i in range(n)
    ]  # fmt: skip


@pytestmark_db
def test_replace_activity_records(conn) -> None:
    store.upsert_activity(conn, synthetic.activity(1001), source="test")
    assert (
        store.replace_activity_records(conn, 1001, _records(10), _laps(2), fit_path="a.fit") == 10
    )
    row = conn.execute("SELECT has_records, fit_path FROM activities WHERE id = 1001").fetchone()
    assert row == {"has_records": True, "fit_path": "a.fit"}
    # replacing again (fewer rows, no fit_path) is idempotent and keeps fit_path
    store.replace_activity_records(conn, 1001, _records(5), _laps(1))
    count = conn.execute("SELECT count(*) AS n FROM activity_records").fetchone()["n"]
    laps = conn.execute("SELECT count(*) AS n FROM activity_laps").fetchone()["n"]
    assert (count, laps) == (5, 1)
    rec = conn.execute("SELECT * FROM activity_records ORDER BY ts LIMIT 1").fetchone()
    assert rec["hr"] == 120 and rec["lat"] == pytest.approx(10.0)
    assert conn.execute("SELECT fit_path FROM activities").fetchone()["fit_path"] == "a.fit"
    store.replace_activity_records(conn, 1001, [], [])
    assert conn.execute("SELECT has_records FROM activities").fetchone()["has_records"] is False


@pytestmark_db
def test_replace_activity_records_needs_activity(conn) -> None:
    with pytest.raises(LookupError):
        store.replace_activity_records(conn, 424242, _records(1), [])


@pytestmark_db
def test_upsert_wellness_full_night(conn) -> None:
    result = store.upsert_wellness(
        conn, D, sleep=synthetic.sleep(D), hrv=synthetic.hrv(D), stats=synthetic.stats(D)
    )
    assert result.sleep_arrived is True and result.sleep_synced_at is not None
    row = conn.execute("SELECT * FROM wellness_daily WHERE wake_date = %s", (D,)).fetchone()
    assert row["sleep_score"] == 80 and row["sleep_s"] > 0
    assert row["deep_s"] + row["light_s"] + row["rem_s"] == row["sleep_s"]
    assert row["sleep_end_utc"] > row["sleep_start_utc"]
    assert (row["hrv_night_avg"], row["hrv_status"]) == (60, "BALANCED")
    assert (row["hrv_balanced_low"], row["hrv_balanced_high"]) == (50, 70)
    assert (row["steps"], row["resting_hr"], row["stress_avg"]) == (8000, 50, 30)
    assert row["body_battery_charged"] == 60
    assert row["raw_sleep"]["dailySleepDTO"]["calendarDate"] == D.isoformat()
    levels = conn.execute("SELECT count(*) AS n FROM sleep_levels WHERE wake_date = %s", (D,))
    readings = conn.execute("SELECT count(*) AS n FROM hrv_readings WHERE wake_date = %s", (D,))
    assert levels.fetchone()["n"] == 7 and readings.fetchone()["n"] == 4


@pytestmark_db
def test_upsert_wellness_is_idempotent(conn) -> None:
    args = {"sleep": synthetic.sleep(D), "hrv": synthetic.hrv(D), "stats": synthetic.stats(D)}
    first = store.upsert_wellness(conn, D, **args)
    snapshot = conn.execute("SELECT * FROM wellness_daily").fetchall()
    levels = conn.execute("SELECT * FROM sleep_levels ORDER BY start_utc").fetchall()
    second = store.upsert_wellness(conn, D, **args)
    assert second.sleep_arrived is False
    assert second.sleep_synced_at == first.sleep_synced_at
    after = conn.execute("SELECT * FROM wellness_daily").fetchall()
    strip = lambda rows: [{k: v for k, v in r.items() if k != "updated_at"} for r in rows]  # noqa: E731
    assert strip(after) == strip(snapshot)
    assert conn.execute("SELECT * FROM sleep_levels ORDER BY start_utc").fetchall() == levels


@pytestmark_db
def test_sleep_synced_at_is_set_only_when_real_sleep_first_arrives(conn) -> None:
    pending = store.upsert_wellness(conn, D, sleep=synthetic.sleep(D, synced=False))
    assert pending.sleep_arrived is False and pending.sleep_synced_at is None
    row = conn.execute("SELECT sleep_s, sleep_synced_at FROM wellness_daily").fetchone()
    assert row == {"sleep_s": None, "sleep_synced_at": None}

    when = datetime(2030, 1, 2, 7, 30, tzinfo=UTC)
    arrived = store.upsert_wellness(conn, D, sleep=synthetic.sleep(D), synced_at=when)
    assert arrived.sleep_arrived is True and arrived.sleep_synced_at == when

    later = store.upsert_wellness(conn, D, sleep=synthetic.sleep(D, score=90))
    assert later.sleep_arrived is False and later.sleep_synced_at == when
    assert conn.execute("SELECT sleep_score FROM wellness_daily").fetchone()["sleep_score"] == 90

    # a placeholder after real data neither erases the night nor its raw payload
    store.upsert_wellness(conn, D, sleep=synthetic.sleep(D, synced=False))
    row = conn.execute("SELECT sleep_s, raw_sleep FROM wellness_daily").fetchone()
    assert row["sleep_s"] > 0 and "sleepLevels" in row["raw_sleep"]
    assert conn.execute("SELECT count(*) AS n FROM sleep_levels").fetchone()["n"] == 7


@pytestmark_db
def test_wake_date_keys_last_night_to_today(conn) -> None:
    yesterday, today = D - timedelta(days=1), D
    store.upsert_wellness(conn, yesterday, sleep=synthetic.sleep(yesterday, score=60))
    store.upsert_wellness(conn, today, sleep=synthetic.sleep(today, score=85))
    row = conn.execute(
        "SELECT sleep_score, sleep_end_utc FROM wellness_daily WHERE wake_date = %s", (today,)
    ).fetchone()
    assert row["sleep_score"] == 85
    assert row["sleep_end_utc"].date() == today  # the night that ended on the wake date
    stages = conn.execute(
        "SELECT DISTINCT wake_date FROM sleep_levels WHERE start_utc >= %s",
        (datetime.combine(today, datetime.min.time(), tzinfo=UTC) - timedelta(hours=3),),
    ).fetchall()
    assert [r["wake_date"] for r in stages] == [today]


@pytestmark_db
def test_steps_never_decrease(conn) -> None:
    store.upsert_wellness(conn, D, stats=synthetic.stats(D, steps=5000))
    store.upsert_wellness(conn, D, stats=synthetic.stats(D, steps=3000))
    assert conn.execute("SELECT steps FROM wellness_daily").fetchone()["steps"] == 5000
    store.upsert_wellness(conn, D, stats=synthetic.stats(D, steps=None))
    assert conn.execute("SELECT steps FROM wellness_daily").fetchone()["steps"] == 5000
    store.upsert_wellness(conn, D, stats=synthetic.stats(D, steps=9000))
    assert conn.execute("SELECT steps FROM wellness_daily").fetchone()["steps"] == 9000


@pytestmark_db
def test_missing_values_do_not_overwrite(conn) -> None:
    store.upsert_wellness(conn, D, hrv=synthetic.hrv(D), stats=synthetic.stats(D))
    store.upsert_wellness(conn, D, hrv={"error": "HTTPError"}, stats=None)
    row = conn.execute("SELECT hrv_night_avg, resting_hr, raw_hrv FROM wellness_daily").fetchone()
    assert row["hrv_night_avg"] == 60 and row["resting_hr"] == 50
    assert row["raw_hrv"]["hrvSummary"]["status"] == "BALANCED"


@pytestmark_db
def test_hrv_readings_replaced_per_night(conn) -> None:
    store.upsert_wellness(conn, D, hrv=synthetic.hrv(D, values=[50, 51, 52, 53, 54]))
    store.upsert_wellness(conn, D, hrv=synthetic.hrv(D, values=[60, 61]))
    values = [r["value_ms"] for r in conn.execute("SELECT value_ms FROM hrv_readings ORDER BY ts")]
    assert values == [60, 61]


@pytestmark_db
def test_upsert_personal_records_is_idempotent(conn) -> None:
    pr = store.PersonalRecord("garmin_3", "5k", 1500.0, 77, "Race", D, "garmin_pr")
    assert store.upsert_personal_records(conn, [pr]) == 1
    store.upsert_personal_records(conn, [pr])
    better = store.PersonalRecord("garmin_3", "5k", 1450.0, 78, "Race 2", D, "garmin_pr")
    store.upsert_personal_records(conn, [better])
    rows = conn.execute("SELECT * FROM personal_records").fetchall()
    assert len(rows) == 1 and rows[0]["value"] == 1450.0 and rows[0]["activity_id"] == 78
    assert store.upsert_personal_records(conn, []) == 0


@pytestmark_db
def test_insert_event_dedupes(conn) -> None:
    first = store.insert_sleep_arrived(conn, D)
    assert isinstance(first, int)
    assert store.insert_sleep_arrived(conn, D) is None
    ev = conn.execute("SELECT * FROM events").fetchone()
    assert ev["kind"] == "sleep_arrived" and ev["dedupe_key"] == f"sleep:{D.isoformat()}"
    assert ev["payload"] == {"wake_date": D.isoformat()} and ev["processed_at"] is None

    assert store.insert_new_activity(conn, 1001, "running", True) is not None
    assert store.insert_new_activity(conn, 1001, "running", True) is None
    # without a dedupe key every insert is new
    assert store.insert_event(conn, "note") != store.insert_event(conn, "note")
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 4


@pytestmark_db
def test_sync_state_roundtrip(conn) -> None:
    assert store.get_sync_state(conn, "garmin.cursor") is None
    assert store.get_sync_state(conn, "garmin.cursor", {"x": 1}) == {"x": 1}
    store.set_sync_state(conn, "garmin.cursor", {"last": D, "n": 3})
    assert store.get_sync_state(conn, "garmin.cursor") == {"last": D.isoformat(), "n": 3}
    store.set_sync_state(conn, "garmin.cursor", [1, 2])
    assert store.get_sync_state(conn, "garmin.cursor") == [1, 2]
    assert conn.execute("SELECT count(*) AS n FROM sync_state").fetchone()["n"] == 1


@pytestmark_db
def test_store_calls_join_an_outer_transaction(conn) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), conn.transaction():
        store.upsert_activity(conn, synthetic.activity(1001), source="test")
        store.insert_event(conn, "x", dedupe_key="k")
        raise Boom
    assert conn.execute("SELECT count(*) AS n FROM activities").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0
