"""Synthetic per-second activities for the analysis tests. All values are made up.

An activity is a list of :class:`Phase` objects run along a straight north-south line from a
made-up origin; each phase has a speed, heart rate (constant or a linear ramp), cadence,
power, breathing rate and grade. A phase with ``paused=True`` emits no records (the watch was
paused) and a phase with ``speed=0`` is standing still.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from tests import synthetic
from thresy import store
from thresy.analysis.data import RECORD_COLUMNS, prepare
from thresy.fit import Lap, Record

T0 = datetime(2030, 3, 4, 8, 0, tzinfo=UTC)
ORIGIN = (10.0, 20.0)  # a made-up position
M_PER_DEG = 111320.0


@dataclass
class Phase:
    seconds: int
    speed: float = 3.0
    hr: float | tuple[float, float] = 140.0
    cad: float | None = 170.0
    power: float | None = 250.0
    grade: float = 0.0
    direction: int = 1  # +1 north, -1 south
    resp: float | None = 30.0
    paused: bool = False
    east_m: float = 0.0  # constant offset to the east (a parallel track)


def records(
    phases: list[Phase],
    *,
    t0: datetime = T0,
    origin: tuple[float, float] = ORIGIN,
    alt0: float = 100.0,
    noise: float = 0.0,
    seed: int = 1,
) -> list[Record]:
    rng = random.Random(seed)
    out: list[Record] = []
    t = 0
    north = 0.0
    dist = 0.0
    alt = alt0
    for ph in phases:
        if ph.paused:
            t += ph.seconds
            continue
        for k in range(ph.seconds):
            if isinstance(ph.hr, tuple):
                a, b = ph.hr
                hr = a + (b - a) * k / max(ph.seconds - 1, 1)
            else:
                hr = ph.hr
            hr += rng.uniform(-noise, noise)
            lat = origin[0] + north / M_PER_DEG
            lon = origin[1] + ph.east_m / M_PER_DEG
            out.append(
                Record(
                    ts=t0 + timedelta(seconds=t),
                    elapsed_s=float(t),
                    lat=lat,
                    lon=lon,
                    alt_m=alt,
                    dist_m=dist,
                    speed_mps=ph.speed,
                    hr=round(hr),
                    cadence_spm=ph.cad,
                    power_w=ph.power,
                    resp_rate=ph.resp,
                    temp_c=10.0,
                )
            )
            north += ph.speed * ph.direction
            dist += ph.speed
            alt += ph.speed * ph.grade
            t += 1
    return out


def frame(recs: list[Record]) -> pd.DataFrame:
    """A prepared record frame, as :func:`thresy.analysis.data.load_records` returns it."""
    rows = [
        {short: getattr(r, db) for db, short in RECORD_COLUMNS.items() if db != "elapsed_s"}
        | {"t": r.elapsed_s}
        for r in recs
    ]
    return prepare(pd.DataFrame(rows))


def lap(
    lap_no: int,
    start: datetime,
    seconds: float,
    dist: float,
    *,
    trigger: str = "manual",
    intensity: str | None = "active",
    wkt_step: int | None = None,
    power: float | None = None,
) -> Lap:
    return Lap(
        lap_no=lap_no,
        start_utc=start,
        duration_s=seconds,
        elapsed_s=seconds,
        dist_m=dist,
        avg_speed_mps=dist / seconds if seconds else None,
        max_speed_mps=None,
        avg_hr=None,
        max_hr=None,
        avg_cadence_spm=None,
        avg_power_w=power,
        ascent_m=None,
        descent_m=None,
        avg_temp_c=None,
        intensity=intensity,
        trigger=trigger,
        wkt_step=wkt_step,
    )


def laps_for(
    phases: list[Phase], triggers: list[str], *, t0: datetime = T0, **kw: Any
) -> list[Lap]:
    """One lap per phase, ended by the given triggers."""
    out = []
    t = 0
    for n, (ph, trig) in enumerate(zip(phases, triggers, strict=True), start=1):
        out.append(lap(n, t0 + timedelta(seconds=t), ph.seconds, ph.seconds * ph.speed,
                       trigger=trig, power=ph.power, **kw))  # fmt: skip
        t += ph.seconds
    return out


def put_activity(
    conn: Any,
    activity_id: int,
    recs: list[Record],
    laps: list[Lap] | None = None,
    *,
    t0: datetime = T0,
    type_key: str = "running",
    name: str = "Synthetic Run",
    utc_offset_h: int = 1,
) -> None:
    """Store a synthetic activity with its records and laps."""
    local = t0 + timedelta(hours=utc_offset_h)
    dist = recs[-1].dist_m if recs else 0.0
    duration = recs[-1].elapsed_s if recs else 0.0
    payload = synthetic.activity(
        activity_id,
        type_key=type_key,
        start_local=local.strftime("%Y-%m-%d %H:%M:%S"),
        start_gmt=t0.strftime("%Y-%m-%d %H:%M:%S"),
        distance=dist,
        duration=duration,
        moving=duration,
        name=name,
    )
    store.upsert_activity(conn, payload, source="test")
    store.replace_activity_records(conn, activity_id, recs, laps or [])


# Ready-made sessions --------------------------------------------------------------------------

WARMUP_S = 600
REP_S = 180
REC_S = 60
N_REPS = 6


def interval_phases(*, flat_power: bool = True) -> list[Phase]:
    """10 min warm-up, 6 x (3 min fast, 1 min jog), 6 min cool-down."""
    phases = [Phase(WARMUP_S, speed=3.0, hr=(120, 135), cad=168, power=240, resp=25)]
    for k in range(N_REPS):
        phases.append(Phase(REP_S, speed=4.6, hr=(140, 166 + k), cad=184, power=420, resp=38))
        phases.append(Phase(REC_S, speed=2.8, hr=(166 + k, 146 + k), cad=160, power=200, resp=34))
    phases.append(Phase(360, speed=3.0, hr=(150, 130), cad=166, power=240, resp=28))
    return phases


def rep_starts() -> list[int]:
    return [WARMUP_S + k * (REP_S + REC_S) for k in range(N_REPS)]


def hill_out_and_back(laps: int = 1) -> list[Phase]:
    """Flat 500 m, 1000 m up at 8 %, flat 200 m, back down and flat 700 m home; ``laps`` times."""
    one = [
        Phase(167, speed=3.0, hr=130),  # ~500 m flat
        Phase(400, speed=2.5, hr=(140, 160), grade=0.08),  # 1000 m up, +80 m
        Phase(67, speed=3.0, hr=150),  # ~200 m flat
        Phase(67, speed=3.0, hr=145, direction=-1),  # back over the flat top
        Phase(286, speed=3.5, hr=130, grade=-0.08, direction=-1),  # ~1000 m down
        Phase(143, speed=3.5, hr=125, direction=-1),  # ~500 m flat home
    ]
    return one * laps
