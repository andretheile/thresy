from __future__ import annotations

from dataclasses import asdict
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tests.analysis.synth import (
    N_REPS,
    REC_S,
    REP_S,
    T0,
    Phase,
    frame,
    interval_phases,
    laps_for,
    put_activity,
    records,
    rep_starts,
)
from thresy.analysis import intervals as iv
from thresy.analysis.data import Activity, load_laps, load_records

ACT = Activity(7, T0, None, date(2030, 3, 4), "running", True, "Intervals", None, None, None,
               None, None, None, None, None, None, True)  # fmt: skip


def _laps_frame(phases: list[Phase], triggers: list[str], **kw) -> pd.DataFrame:
    laps = pd.DataFrame([asdict(lap) for lap in laps_for(phases, triggers, **kw)])
    laps["start_utc"] = pd.to_datetime(laps["start_utc"], utc=True)
    laps["t_start"] = (laps["start_utc"] - pd.Timestamp(T0)).dt.total_seconds()
    laps["t_end"] = laps["t_start"] + laps["elapsed_s"]
    return laps


def test_two_means() -> None:
    thr, lo, hi, *_ = iv.two_means(np.array([1, 1.1, 0.9, 5, 5.2, 4.8]))
    assert lo == pytest.approx(1.0) and hi == pytest.approx(5.0)
    assert 1.1 < thr < 4.8
    assert iv.two_means(np.array([3.0, 3.0])) is None


@pytest.mark.parametrize("signal", ["speed", "power"])
def test_known_intervals_come_out_of_the_records(signal: str) -> None:
    df = frame(records(interval_phases(), noise=1.0))
    det = iv.detect_from_records(df, signal=signal)
    assert det.source == "records"
    assert len(det.work) == N_REPS
    for (start, end), expected in zip(det.work, rep_starts(), strict=True):
        assert start == pytest.approx(expected, abs=2)
        assert end - start == pytest.approx(REP_S, abs=3)


def test_rep_metrics() -> None:
    df = frame(records(interval_phases()))
    result = iv.analyze(ACT, df, pd.DataFrame(), source="records")
    reps = result["reps"]
    assert len(reps) == N_REPS
    first = reps[0]
    assert first["pace_s_per_km"] == pytest.approx(1000 / 4.6, abs=3)
    assert first["dist_m"] == pytest.approx(REP_S * 4.6, abs=15)
    assert first["cadence_spm"] == pytest.approx(184, abs=1)
    assert first["stride_m"] == pytest.approx(4.6 / (184 / 60), abs=0.03)
    assert first["resp_avg"] == pytest.approx(38, abs=0.5)
    assert first["power_w"] == pytest.approx(420, abs=5)
    assert first["recovery_s"] == pytest.approx(REC_S, abs=3)
    # HR ramps 140 -> 166 over the rep: the last 30 s average about 163.8, the end is 166
    assert first["hr_last30"] == pytest.approx(163.8, abs=1)
    assert first["hr_end"] == pytest.approx(166, abs=1)
    # recovery: 166 -> 146 over 60 s, then the next rep starts at 140
    assert first["hr_drop_60"] == pytest.approx(20 + 0, abs=6)
    assert reps[-1]["recovery_s"] is None
    summary = result["summary"]
    assert summary["n"] == N_REPS and summary["hr_first_to_last"] > 0
    assert len(result["recoveries"]) == N_REPS - 1


def test_manual_laps_give_exact_boundaries() -> None:
    phases = interval_phases()
    triggers = ["manual"] * (len(phases) - 1) + ["session_end"]
    df = frame(records(phases))
    det = iv.detect(df, _laps_frame(phases, triggers), "auto")
    assert det.source == "laps"
    assert [s for s, _ in det.work] == rep_starts()
    assert all(e - s == REP_S for s, e in det.work)


def test_workout_laps_use_intensity() -> None:
    phases = interval_phases()
    laps = _laps_frame(phases, ["distance"] * len(phases), wkt_step=1)
    laps["intensity"] = ["warmup"] + ["active", "rest"] * N_REPS + ["cooldown"]
    det = iv.detect(frame(records(phases)), laps)
    assert det.source == "workout"
    assert [s for s, _ in det.work] == rep_starts()


def test_auto_laps_fall_back_to_records() -> None:
    phases = interval_phases()
    df = frame(records(phases))
    det = iv.detect(df, _laps_frame(phases, ["distance"] * len(phases)))
    assert det.source == "records"
    assert len(det.work) == N_REPS


def test_stray_lap_presses_are_not_intervals() -> None:
    phases = [Phase(600, speed=3.0), Phase(300, speed=3.4), Phase(600, speed=3.0),
              Phase(300, speed=3.4), Phase(600, speed=3.0)]  # fmt: skip
    laps = _laps_frame(phases, ["manual", "distance", "distance", "manual", "session_end"])
    assert iv.detect_from_laps(laps) is None


def test_steady_run_has_no_intervals() -> None:
    df = frame(records([Phase(2400, speed=3.0, hr=(130, 150))], noise=1.0))
    result = iv.analyze(ACT, df, pd.DataFrame())
    assert result["reps"] == [] and result["source"] == "none"
    assert "no clear" in result["note"]


def test_matches() -> None:
    rep = {"dist_m": 1040.0, "time_s": 200.0}
    assert iv.matches(rep, 1000, None, 0.15)
    assert not iv.matches(rep, 800, None, 0.15)
    assert iv.matches(rep, None, 180, 0.15)
    assert not iv.matches(rep, None, 120, 0.15)


@pytest.mark.db
def test_compare_sessions_finds_sessions_of_the_same_kind(conn) -> None:
    put_activity(conn, 101, records(interval_phases()), t0=T0)
    # 1 km reps on another day
    km = [Phase(600, speed=3.0)] + [Phase(220, speed=4.6), Phase(120, speed=2.8)] * 4
    km += [Phase(400, speed=3.0)]
    put_activity(conn, 102, records(km), t0=T0.replace(day=10))
    put_activity(conn, 103, records([Phase(2400, speed=3.0)]), t0=T0.replace(day=12))

    result = iv.compare_sessions(conn, rep_m=1000, tolerance=0.1)
    assert [s["activity_id"] for s in result["sessions"]] == [102]
    assert result["sessions"][0]["n_matching"] == 4
    by_time = iv.compare_sessions(conn, rep_s=180, first_n=3)
    session = by_time["sessions"][0]
    assert session["activity_id"] == 101 and session["n_used"] == 3
    assert result["analysed"] == 2  # the steady run is filtered out cheaply


@pytest.mark.db
def test_laps_round_trip_through_the_database(conn) -> None:
    phases = interval_phases()
    triggers = ["manual"] * (len(phases) - 1) + ["session_end"]
    put_activity(conn, 104, records(phases), laps_for(phases, triggers))
    df = load_records(conn, 104)
    laps = load_laps(conn, 104, df["ts"].iloc[0])
    assert list(laps["t_start"][:2]) == [0.0, 600.0]
    det = iv.detect(df, laps)
    assert det.source == "laps" and [s for s, _ in det.work] == rep_starts()
