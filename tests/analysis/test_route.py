from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.analysis.synth import T0, Phase, frame, put_activity, records
from thresy.analysis import route as rt
from thresy.analysis.data import Activity, get_activity


def _act(activity_id: int) -> Activity:
    return Activity(activity_id, T0, None, date(2030, 3, 4), "running", True, "x", None, None,
                    None, None, None, None, None, None, None, True)  # fmt: skip


def course(*, slow_second_km: bool = False, east_m: float = 0.0, stop_s: int = 0) -> list[Phase]:
    """2 km out, 2 km back at 5:00/km (optionally 6:00/km over the second km)."""
    v = 1000 / 300
    second = 1000 / 360 if slow_second_km else v
    phases = [
        Phase(300, speed=v, hr=140, east_m=east_m),
        Phase(round(1000 / second), speed=second, hr=150, east_m=east_m),
    ]
    if stop_s:
        phases.append(Phase(stop_s, speed=0.0, hr=120, east_m=east_m))
    phases += [Phase(600, speed=v, hr=145, direction=-1, east_m=east_m)]
    return phases


def test_compare_aligns_an_out_and_back_course() -> None:
    a = frame(records(course()))
    b = frame(records(course(slow_second_km=True, east_m=8.0, stop_s=45), seed=2))
    result = rt.compare_frames(_act(1), a, _act(2), b)
    assert result["course_length_m"] == pytest.approx(4000, abs=10)
    assert result["on_route_b"] > 0.99
    assert result["delta_s"] == pytest.approx(60 + 45, abs=4)
    secs = {s["n"]: s for s in result["sections"]}
    assert secs[1]["delta_s"] == pytest.approx(0, abs=2)
    assert secs[2]["delta_s"] == pytest.approx(60, abs=2)  # slower
    # the stop at the turnaround counts in the section that starts there; the way back is
    # matched in order, not to the way out
    assert secs[3]["delta_s"] == pytest.approx(45, abs=2)
    assert secs[4]["delta_s"] == pytest.approx(0, abs=2)
    assert secs[4]["cum_delta_s"] == pytest.approx(105, abs=4)
    assert result["most_lost"][0] == 2
    assert result["stopped_b_s"] == pytest.approx(45, abs=2)
    assert result["stopped_a_s"] == 0
    assert result["hr_b"] < result["hr_a"] + 5


def test_compare_rejects_a_different_course() -> None:
    a = frame(records(course()))
    b = frame(records(course(), origin=(11.0, 21.0)))
    with pytest.raises(ValueError, match="course"):
        rt.compare_frames(_act(1), a, _act(2), b)


def test_project_progress_handles_off_route_points() -> None:
    import numpy as np

    # the course runs 1 km north; the other track first crosses it from west to east at 600 m
    # (which must not lock the progress there), then joins it at the start and follows it
    ys = np.arange(0.0, 1001.0, 10.0)
    ref = np.column_stack([np.zeros_like(ys), ys])
    crossing = np.column_stack([np.arange(-300.0, 301.0, 10.0), np.full(61, 600.0)])
    follow = np.column_stack([np.full(len(ys), 3.0), ys])
    follow[50] = [100.0, 500.0]  # one point off the course
    prog, off = rt.project_progress(ref, ys, np.vstack([crossing, follow]))
    assert np.isnan(prog[:61]).all()
    on = prog[61:]
    assert on[0] == pytest.approx(0, abs=1) and on[-1] == pytest.approx(1000, abs=1)
    assert np.isnan(on[50]) and off[61 + 50] == pytest.approx(100)
    assert on[49] == pytest.approx(490, abs=1) and on[51] == pytest.approx(510, abs=1)


@pytest.mark.db
def test_find_same_course_and_compare_from_the_database(conn) -> None:
    put_activity(conn, 301, records(course()))
    put_activity(conn, 302, records(course(slow_second_km=True, east_m=5.0)),
                 t0=T0 + timedelta(days=365))  # fmt: skip
    put_activity(conn, 303, records(course()[:1]), t0=T0 + timedelta(days=366))  # first km only
    put_activity(conn, 304, records(course(), origin=(11.0, 21.0)), t0=T0 + timedelta(days=367))
    ref = get_activity(conn, 301)
    found = rt.find_same_course(conn, ref, min_coverage=0.8)
    assert [m["activity_id"] for m in found] == [302]
    assert found[0]["coverage"] > 0.95
    partial = rt.find_same_course(conn, ref, min_coverage=0.2)
    assert {m["activity_id"] for m in partial} == {302, 303}
    result = rt.compare(conn, ref, get_activity(conn, 302))
    assert result["delta_s"] == pytest.approx(60, abs=4)
