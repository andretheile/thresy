from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from tests.fitbuilder import LAP, RECORD, SESSION, FitBuilder, deg_to_semicircles, fit_time
from thresy import fit

T0 = datetime(2030, 1, 2, 6, 0, tzinfo=UTC)


def test_semicircles_to_deg() -> None:
    assert fit.semicircles_to_deg(None) is None
    assert fit.semicircles_to_deg(2**30) == pytest.approx(90.0)
    assert fit.semicircles_to_deg(-(2**31)) == pytest.approx(-180.0)


def test_steps_per_minute_doubles_and_adds_fraction() -> None:
    assert fit.steps_per_minute(None) is None
    assert fit.steps_per_minute(80) == 160
    assert fit.steps_per_minute(80, 0.5) == 161
    assert fit.steps_per_minute(0, None) == 0


def test_as_utc() -> None:
    naive = datetime(2030, 1, 1, 12, 0)
    assert fit.as_utc(naive) == datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    assert fit.as_utc(None) is None


def test_intensity_name() -> None:
    assert fit.intensity_name(None) is None
    assert fit.intensity_name("active") == "active"
    assert fit.intensity_name(5) == "interval"
    assert fit.intensity_name(4) == "recovery"
    assert fit.intensity_name(42) == "42"


def test_record_from_values_prefers_enhanced_fields_and_uses_field_108() -> None:
    values = {
        "timestamp": datetime(2030, 1, 2, 6, 0, 10),
        "position_lat": 2**30,
        "position_long": -(2**30),
        "altitude": 100.0,
        "enhanced_altitude": 101.0,
        "speed": 2.0,
        "enhanced_speed": 3.0,
        "distance": 25.0,
        "heart_rate": 120,
        "cadence": 85,
        "fractional_cadence": 0.5,
        "power": 250,
        "temperature": 20,
        "_resp": 30.5,
        "enhanced_respiration_rate": 99.0,  # ignored: field 108 wins
    }
    r = fit.record_from_values(values, T0)
    assert r.ts == T0 + timedelta(seconds=10)
    assert r.elapsed_s == 10
    assert r.lat == pytest.approx(90.0)
    assert r.lon == pytest.approx(-90.0)
    assert r.alt_m == 101.0
    assert r.speed_mps == 3.0
    assert r.dist_m == 25.0
    assert r.hr == 120
    assert r.cadence_spm == 171.0
    assert r.power_w == 250
    assert r.temp_c == 20
    assert r.resp_rate == 30.5


def test_record_from_values_falls_back_and_handles_missing() -> None:
    r = fit.record_from_values(
        {"timestamp": datetime(2030, 1, 2, 6, 0), "altitude": 5.0, "speed": 1.5}, T0
    )
    assert (r.alt_m, r.speed_mps) == (5.0, 1.5)
    assert r.lat is r.lon is r.hr is r.cadence_spm is r.resp_rate is None


def test_record_from_values_rpm_sports_have_no_step_cadence() -> None:
    values = {"timestamp": datetime(2030, 1, 2, 6, 0), "cadence": 90}
    assert fit.record_from_values(values, T0, cadence_is_steps=False).cadence_spm is None


def test_lap_from_values() -> None:
    lap = fit.lap_from_values(
        2,
        {
            "start_time": datetime(2030, 1, 2, 6, 5),
            "total_timer_time": 300.0,
            "total_elapsed_time": 310.0,
            "total_distance": 1000.0,
            "avg_speed": 3.0,
            "enhanced_avg_speed": 3.3,
            "enhanced_max_speed": 4.0,
            "avg_heart_rate": 150,
            "max_heart_rate": 160,
            "avg_running_cadence": 85,
            "avg_fractional_cadence": 0.25,
            "avg_power": 260,
            "total_ascent": 10,
            "total_descent": 12,
            "avg_temperature": 18,
            "intensity": 5,
            "lap_trigger": "distance",
            "wkt_step_index": 3,
        },
    )
    assert lap.lap_no == 2
    assert lap.start_utc == datetime(2030, 1, 2, 6, 5, tzinfo=UTC)
    assert (lap.duration_s, lap.elapsed_s, lap.dist_m) == (300.0, 310.0, 1000.0)
    assert (lap.avg_speed_mps, lap.max_speed_mps) == (3.3, 4.0)
    assert lap.avg_cadence_spm == 170.5
    assert (lap.intensity, lap.trigger, lap.wkt_step) == ("interval", "distance", 3)


def test_lap_without_values() -> None:
    lap = fit.lap_from_values(1, {"avg_running_cadence": 0})
    assert lap.avg_cadence_spm is None  # the old parser treated 0 as "no cadence"
    assert lap.intensity is lap.trigger is lap.wkt_step is lap.start_utc is None


def _synthetic_fit(*, sport: int = 1, duplicate: bool = False) -> bytes:
    b = FitBuilder()
    for i in range(5):
        ts = fit_time(T0) + i
        b.message(
            RECORD,
            {
                253: ts,
                0: deg_to_semicircles(10.0 + i * 1e-4),
                1: deg_to_semicircles(20.0),
                5: i * 300,  # 3 m per second
                73: 3000,  # 3.0 m/s
                78: (100 + 500) * 5,  # 100 m
                3: 120 + i,
                4: 85,
                53: 64,  # 0.5
                7: 250,
                13: 15,
                108: 2850,  # 28.5 breaths/min
            },
        )
        if duplicate and i == 2:
            b.message(RECORD, {253: ts, 3: 199})
    b.message(
        LAP,
        {
            254: 0,
            253: fit_time(T0) + 4,
            2: fit_time(T0),
            7: 4000,
            8: 4000,
            9: 1200,
            110: 3000,
            111: 3500,
            15: 122,
            16: 124,
            17: 85,
            80: 64,
            19: 250,
            21: 1,
            22: 2,
            23: 0,
            24: 0,
            25: sport,
            50: 15,
            71: None,
        },
    )
    b.message(SESSION, {253: fit_time(T0) + 4, 2: fit_time(T0), 5: sport, 6: 0})
    return b.build()


def test_parse_fit_synthetic_file(tmp_path) -> None:
    path = tmp_path / "a.fit"
    path.write_bytes(_synthetic_fit())
    data = fit.parse_fit(path)
    assert data.sport == "running"
    assert len(data.records) == 5
    first, last = data.records[0], data.records[-1]
    assert first.ts == T0 and last.elapsed_s == 4
    assert first.lat == pytest.approx(10.0, abs=1e-6)
    assert first.lon == pytest.approx(20.0, abs=1e-6)
    assert last.dist_m == pytest.approx(12.0)
    assert first.speed_mps == pytest.approx(3.0)
    assert first.alt_m == pytest.approx(100.0)
    assert [r.hr for r in data.records] == [120, 121, 122, 123, 124]
    assert first.cadence_spm == pytest.approx(171.0)
    assert first.resp_rate == pytest.approx(28.5)
    assert (first.power_w, first.temp_c) == (250, 15)

    assert len(data.laps) == 1
    lap = data.laps[0]
    assert lap.lap_no == 1 and lap.start_utc == T0
    assert lap.dist_m == pytest.approx(12.0)
    assert lap.avg_speed_mps == pytest.approx(3.0)
    assert lap.avg_cadence_spm == pytest.approx(171.0)
    assert (lap.intensity, lap.trigger, lap.wkt_step) == ("active", "manual", None)


def test_parse_fit_accepts_bytes_and_drops_duplicate_timestamps() -> None:
    data = fit.parse_fit(_synthetic_fit(duplicate=True))
    assert len(data.records) == 5
    assert data.records[2].hr == 122  # the first sample for a timestamp wins


def test_parse_fit_cycling_has_no_step_cadence() -> None:
    data = fit.parse_fit(_synthetic_fit(sport=2))
    assert data.sport == "cycling"
    assert all(r.cadence_spm is None for r in data.records)


def test_parse_fit_rejects_garbage() -> None:
    with pytest.raises(fit.FitError):
        fit.parse_fit(b"not a fit file at all")


def test_fit_bytes_from_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
        z.writestr("123_ACTIVITY.fit", b"FITDATA")
    assert fit.fit_bytes_from_zip(buf.getvalue()) == b"FITDATA"
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(fit.FitError):
        fit.fit_bytes_from_zip(empty.getvalue())


def test_columns_match_contract() -> None:
    assert fit.RECORD_COLUMNS == (
        "ts", "elapsed_s", "lat", "lon", "alt_m", "dist_m", "speed_mps", "hr",
        "cadence_spm", "power_w", "resp_rate", "temp_c",
    )  # fmt: skip
    assert fit.LAP_COLUMNS[0] == "lap_no" and fit.LAP_COLUMNS[-1] == "wkt_step"
