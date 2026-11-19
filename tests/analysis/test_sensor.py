from __future__ import annotations

import random

import pytest

from tests.analysis.synth import Phase, frame, records
from thresy.analysis import sensor


def _run(minutes: int = 30, seed: int = 3) -> object:
    """A steady run: heart rate about 150, cadence about 172 varying independently."""
    rng = random.Random(seed)
    phases = [
        Phase(60, speed=3.2, hr=150 + rng.uniform(-2, 2), cad=172 + rng.uniform(-3, 3))
        for _ in range(minutes)
    ]
    return frame(records(phases, noise=1.0, seed=seed))


def test_clean_run_is_ok() -> None:
    result = sensor.check(_run())
    assert result["verdict"] == "ok"
    assert result["cadence_lock"] == [] and result["spikes"] == [] and result["jumps"] == []
    assert result["avg_hr_clean"] == pytest.approx(result["avg_hr"], abs=0.1)


def test_cadence_locked_stretch_is_flagged() -> None:
    df = _run()
    rng = random.Random(4)
    locked = (df["t"] >= 600) & (df["t"] < 1200)  # minutes 10 to 20
    # the wrist sensor reports the step rate: heart rate equals cadence (which wanders)
    df.loc[locked, "cad"] = [
        175 + 5 * ((i // 40) % 3) + rng.uniform(-1, 1) for i in range(int(locked.sum()))
    ]
    df.loc[locked, "hr"] = (
        df.loc[locked, "cad"] + [rng.choice([-1, 0, 1]) for _ in range(int(locked.sum()))]
    ).round()
    result = sensor.check(df)
    assert result["verdict"] == "unreliable"
    (lock,) = result["cadence_lock"]
    assert lock["confidence"] == "high"
    assert lock["t_start"] == pytest.approx(600, abs=60)
    assert lock["duration_s"] == pytest.approx(600, abs=90)
    assert lock["edge_jump_bpm"] >= 20  # 150 -> 175 at the edges
    assert result["avg_hr_clean"] < result["avg_hr"]
    assert result["jumps"]  # the abrupt switch is also an impossible jump


def test_spike_and_dropout() -> None:
    df = _run()
    spike = (df["t"] >= 900) & (df["t"] < 908)
    df.loc[spike, "hr"] = 185
    gap = (df["t"] >= 1500) & (df["t"] < 1560)
    df.loc[gap, "hr"] = float("nan")
    result = sensor.check(df)
    assert result["verdict"] == "suspect"
    (s,) = result["spikes"]
    assert s["max_hr"] == 185 and s["duration_s"] == 8
    assert result["max_hr"] == 185 and result["max_hr_clean"] < 160
    assert result["dropouts"] == [{"t": 1500, "duration_s": 60}]


def test_hard_effort_near_cadence_is_not_a_high_confidence_lock() -> None:
    rng = random.Random(5)
    # heart rate slowly rising 165 -> 175 while the cadence wanders independently around 172
    phases = [Phase(1800, speed=4.0, hr=(165, 175), cad=None)]
    df = frame(records(phases, seed=5))
    df["cad"] = [172 + rng.uniform(-4, 4) for _ in range(len(df))]
    result = sensor.check(df)
    assert all(x["confidence"] != "high" for x in result["cadence_lock"])
    assert result["verdict"] != "unreliable"


def test_no_heart_rate() -> None:
    df = frame(records([Phase(600, speed=3.0)]))
    df["hr"] = float("nan")
    result = sensor.check(df)
    assert result["has_hr"] is False and result["verdict"] == "none"


def test_level_shift_after_a_dropout_at_the_same_effort() -> None:
    rng = random.Random(6)
    df = _run(40)
    locked = df["t"] < 1200  # the first 20 minutes the sensor reads the step rate
    df.loc[locked, "hr"] = (
        df.loc[locked, "cad"] + [rng.uniform(-1, 1) for _ in range(int(locked.sum()))]
    ).round()
    df.loc[(df["t"] >= 1200) & (df["t"] < 1260), "hr"] = float("nan")  # a dropout
    df.loc[df["t"] >= 1260, "hr"] = 135.0
    result = sensor.check(df)
    assert result["verdict"] == "unreliable"
    (shift,) = result["level_shifts"]
    assert shift["hr_before"] == pytest.approx(172, abs=4) and shift["hr_after"] == 135
    assert shift["near_cadence_before"] > 0.9 and shift["near_cadence_after"] < 0.1
    assert shift["t"] == pytest.approx(1230, abs=60)  # around the dropout (1200-1260 s)


def test_no_level_shift_when_the_effort_changes_or_the_runner_stops() -> None:
    from tests.analysis.synth import interval_phases

    assert sensor.check(frame(records(interval_phases())))["level_shifts"] == []
    stop = [Phase(900, speed=3.5, hr=165, power=380), Phase(90, paused=True),
            Phase(900, speed=3.5, hr=(125, 165), power=380)]  # fmt: skip
    assert sensor.check(frame(records(stop)))["level_shifts"] == []
