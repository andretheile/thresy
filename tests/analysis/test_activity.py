from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tests.analysis.synth import Phase, frame, hill_out_and_back, records
from thresy.analysis import activity as act
from thresy.analysis.data import Activity, ascent_descent, first_reach, prepare


def test_prepare_timer_skips_pauses_and_marks_standing() -> None:
    df = frame(records([Phase(100, speed=3.0), Phase(60, paused=True), Phase(20, speed=0.0),
                        Phase(100, speed=3.0)]))  # fmt: skip
    assert df["paused"].sum() == 1
    assert df["t"].iloc[-1] == 279  # wall clock includes the 60 s pause
    assert df["timer"].iloc[-1] == 218  # 279 s minus the 61 s gap
    assert df["still"].sum() == 20
    assert df["still_s"].sum() == 20  # standing from the first still sample to the next run


def test_first_reach_on_plateau() -> None:
    xs = np.array([0.0, 10.0, 10.0, 10.0, 20.0])
    ys = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    assert first_reach(xs, ys, 10.0) == 1.0
    assert first_reach(xs, ys, 15.0) == pytest.approx(3.5)
    assert np.isnan(first_reach(xs, ys, 25.0))


def test_ascent_descent_dead_band_ignores_noise() -> None:
    alt = np.array([100, 100.4, 99.8, 100.3, 101.5, 103, 102.6, 103.2, 100])
    up, down = ascent_descent(alt, 1.0)
    assert up == pytest.approx(3.0)
    assert down == pytest.approx(3.0)


def test_splits_add_up_and_have_the_right_pace() -> None:
    # 2 km at 5:00/km, then 1.5 km at 4:00/km
    df = frame(records([Phase(600, speed=1000 / 300), Phase(360, speed=1000 / 240)]))
    splits = act.splits(df, 1000)
    assert [round(s["dist_m"]) for s in splits] == [1000, 1000, 1000, 496]
    assert [round(s["pace_s_per_km"]) for s in splits[:2]] == [300, 300]
    assert round(splits[2]["pace_s_per_km"]) == 240
    assert sum(s["time_s"] for s in splits) == pytest.approx(df["timer"].iloc[-1], abs=1)


def test_splits_exclude_pauses_but_report_standing() -> None:
    df = frame(records([Phase(300, speed=1000 / 300), Phase(120, paused=True),
                        Phase(30, speed=0.0), Phase(300, speed=1000 / 300)]))  # fmt: skip
    splits = act.splits(df, 1000)
    assert splits[0]["time_s"] == pytest.approx(300, abs=1)
    assert splits[1]["time_s"] == pytest.approx(330, abs=2)  # the 30 s standing count
    stops = act.stops(df)
    assert stops["paused_s"] == 121 and stops["standing_s"] == 30
    kinds = [s["kind"] for s in stops["stops"]]
    assert kinds == ["paused", "standing"]


def test_halves_and_negative_split() -> None:
    df = frame(
        records([Phase(600, speed=1000 / 300, hr=140), Phase(500, speed=1000 / 250, hr=150)])
    )
    h = act.halves(df)
    assert h["negative_split"] is True
    assert h["first"]["pace_s_per_km"] == pytest.approx(300, abs=2)
    assert h["hr_diff"] > 0


def test_hr_drift_and_decoupling() -> None:
    steady = frame(records([Phase(3600, speed=3.0, hr=140)]))
    d = act.hr_drift(steady)
    assert d["drift_bpm"] == pytest.approx(0, abs=0.5)
    assert d["decoupling_pct"] == pytest.approx(0, abs=0.5)

    drifting = frame(records([Phase(3600, speed=3.0, hr=(130, 160))]))
    d = act.hr_drift(drifting)
    # after the 10 min skip: HR 135 -> 160 over 50 min; halves average 141.25 and 153.75
    assert d["drift_bpm"] == pytest.approx(12.5, abs=0.6)
    assert d["decoupling_pct"] == pytest.approx((1 - 141.25 / 153.75) * 100, abs=0.5)

    short = frame(records([Phase(900, speed=3.0, hr=140)]))
    assert "too short" in act.hr_drift(short)["note"]


def test_climbs_found_from_the_altitude_profile() -> None:
    df = frame(records(hill_out_and_back(laps=2)))
    climbs = act.climbs(df)
    assert len(climbs) == 2
    for c in climbs:
        assert c["gain_m"] == pytest.approx(80, abs=4)
        assert c["grade_pct"] == pytest.approx(8, abs=1)
        assert c["dist_m"] == pytest.approx(1000, abs=60)
        assert c["time_s"] == pytest.approx(400, abs=25)
    assert climbs[1]["start_km"] == pytest.approx(3.9, abs=0.1)  # one lap (3.4 km) + 0.5 km
    with_descents = act.climbs(df, descents=True)
    assert [c["direction"] for c in with_descents] == ["up", "down", "up", "down"]
    assert act.climbs(df, min_gain_m=100) == []


def test_turning_points_hysteresis() -> None:
    alt = np.array([0, 5, 10, 8, 12, 30, 25, 18, 19, 5, 6])
    assert act.turning_points(alt, 10.0) == [0, 5, 10]


def test_summarize_has_every_part() -> None:

    df = frame(records(hill_out_and_back()))
    a = Activity(1, None, None, date(2030, 3, 4), "running", True, "x",
                 None, None, None, None, None, 80.0, None, None, None, True)  # fmt: skip
    s = act.summarize(a, df)
    assert set(s) == {"activity", "totals", "splits", "halves", "drift", "climbs", "stops"}
    assert s["totals"]["ascent_m"] == pytest.approx(80, abs=5)
    assert prepare(df.iloc[:0])["timer"].empty
