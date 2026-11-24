"""A synthetic athlete for ``coach web demo`` and ``coach web run --demo``.

Everything here is invented and generated from a fixed seed: two and a bit years of runs
building towards a half marathon, nights of sleep with stages, HRV and resting heart rate, a
training plan, two races, two segments with their efforts, a morning briefing and the state of
the sync. The place is made up (coordinates near 10 N, 20 E are only used to draw shapes; no
map is ever shown) and so is every number.

"Today" is fixed (:data:`DEMO_NOW`), so the pages and their screenshots are the same on every
run. :class:`DemoSource` answers the dashboard's questions in the shapes of the library
functions that :class:`thresy.web.source.DbSource` calls; ``tests/web/test_demo_parity.py``
loads :func:`build` into a database and checks that both sources agree.

Scenarios: ``full`` (the default), ``new`` (an athlete who just connected Garmin: three runs,
last night not synced yet, no plan, races or segments) for the empty states, and ``trouble``
(``full`` with an expired Garmin login, so last night never arrived, and failing backups) for
the notices.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from thresy.analysis.data import Activity, ascent_descent, prepare, smooth_altitude
from thresy.analysis.segments import SegmentDef

DEMO_NOW = datetime(2030, 4, 18, 7, 46, tzinfo=UTC)
"""A Thursday morning: the plan of the week is half done, last night has synced."""
DEMO_TODAY = DEMO_NOW.date()
DEMO_ZONE = ZoneInfo("UTC")
SEED = 20300418
HISTORY_START = date(2028, 1, 3)
NIGHTS_FROM = date(2029, 10, 1)
RECORDS_DAYS = 70
"""Runs of the last this many days have per-second data (older ones are summaries only, as
after an import of old history)."""
LAT0, LON0 = 10.0, 20.0
M_PER_DEG = 111_320.0
SCENARIOS = ("full", "new", "trouble")

NAMES = {
    "en": {
        "easy": "Easy run",
        "easy_strides": "Easy run with strides",
        "recovery": "Recovery jog",
        "threshold": "Threshold 3 × 10 min",
        "intervals": "Intervals 6 × 800 m",
        "long": "Long run",
        "long_fast": "Long run, fast finish",
        "tempo": "Tempo run",
        "rest": "Rest",
        "cycling": "Evening ride",
        "strength": "Strength",
        "race_10k": "Spring 10K",
        "race_half": "Riverside Half Marathon",
        "seg_quarry": "Quarry Hill",
        "seg_ridge": "Ridge Road",
        "first_run": "First run with the new watch",
    },
    "de": {
        "easy": "Lockerer Lauf",
        "easy_strides": "Locker mit Steigerungen",
        "recovery": "Regenerationslauf",
        "threshold": "Schwelle 3 × 10 min",
        "intervals": "Intervalle 6 × 800 m",
        "long": "Langer Lauf",
        "long_fast": "Langer Lauf mit Endbeschleunigung",
        "tempo": "Tempolauf",
        "rest": "Ruhetag",
        "cycling": "Abendrunde Rad",
        "strength": "Kraft",
        "race_10k": "Frühlings-10-km",
        "race_half": "Halbmarathon am Fluss",
        "seg_quarry": "Steinbruch-Anstieg",
        "seg_ridge": "Kammweg",
        "first_run": "Erster Lauf mit der neuen Uhr",
    },
}

PLAN_TEXT = {
    "en": {
        "easy": ("Conversational pace; keep the heart rate low.", "<145"),
        "easy_strides": ("Easy, then 6 × 20 s strides with a full walk back.", "<145"),
        "recovery": ("Very easy, as short as feels right.", "<135"),
        "threshold": ("2 km warm-up, 3 × 10 min at threshold, 2 min jog between.", None),
        "intervals": ("2.5 km warm-up, 6 × 800 m, 2 min jog between.", None),
        "long": ("Steady and relaxed; eat something after 75 minutes.", "<150"),
        "long_fast": ("Steady, the last 5 km at half-marathon pace.", None),
        "tempo": ("2 km warm-up, 5 km at tempo pace, easy home.", None),
        "rest": ("Rest or an easy walk.", None),
    },
    "de": {
        "easy": ("Gesprächstempo, Puls niedrig halten.", "<145"),
        "easy_strides": ("Locker, danach 6 × 20 s Steigerungen, gehend zurück.", "<145"),
        "recovery": ("Sehr locker, so kurz wie es sich richtig anfühlt.", "<135"),
        "threshold": ("2 km einlaufen, 3 × 10 min an der Schwelle, 2 min Trabpause.", None),
        "intervals": ("2,5 km einlaufen, 6 × 800 m, 2 min Trabpause.", None),
        "long": ("Ruhig und gleichmäßig; nach 75 Minuten etwas essen.", "<150"),
        "long_fast": ("Gleichmäßig, die letzten 5 km im Halbmarathontempo.", None),
        "tempo": ("2 km einlaufen, 5 km im Tempo, locker nach Hause.", None),
        "rest": ("Pause oder ein lockerer Spaziergang.", None),
    },
}

PLAN_KIND = {
    "easy": "easy",
    "easy_strides": "strides",
    "recovery": "recovery",
    "threshold": "threshold",
    "intervals": "intervals",
    "long": "long",
    "long_fast": "long",
    "tempo": "tempo",
    "race": "race",
    "rest": "rest",
}


# -- the terrain and the routes --------------------------------------------------------------------


def terrain(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Altitude in metres at (x, y) metres east and north of home."""
    hill = 74 * np.exp(-((x - 1900) ** 2 + (y - 1300) ** 2) / (2 * 750.0**2))
    ridge = 38 * np.exp(-((x + 2600) ** 2) / (2 * 1500.0**2)) * (1 + 0.3 * np.sin(y / 900))
    rolling = 9 * np.sin(x / 650) * np.cos(y / 820) + 5 * np.sin((x + y) / 380)
    return 212 + hill + ridge + rolling


@dataclass(frozen=True)
class Template:
    """A closed loop through home: r(θ) = 1 + Σ a·cos(kθ + φ), scaled to the run's length."""

    harmonics: tuple[tuple[int, float, float], ...]
    rotation: float
    aspect: float = 1.0


TEMPLATES = {
    "river": Template(((2, 0.22, 0.3), (3, 0.08, 1.1), (5, 0.03, 2.0)), 0.2, 1.6),
    "hills": Template(((2, 0.12, 1.4), (3, 0.14, 0.2), (4, 0.05, 2.6)), 0.75, 1.1),
    "ridge": Template(((2, 0.30, 2.2), (3, 0.06, 0.9), (6, 0.03, 0.4)), 2.6, 1.4),
    "park": Template(((2, 0.05, 0.0), (4, 0.04, 1.0)), 1.3, 1.25),
    "town": Template(((2, 0.16, 0.8), (3, 0.10, 2.4), (5, 0.04, 1.7)), 4.0, 1.0),
}


def route_xy(template: Template, length_m: float, n: int = 1500) -> tuple[np.ndarray, np.ndarray]:
    """Points of a loop of about ``length_m`` metres starting and ending at home."""
    theta = np.linspace(0, 2 * np.pi, n)
    r = np.ones_like(theta)
    for k, a, phi in template.harmonics:
        r += a * np.cos(k * theta + phi)
    x = r * np.cos(theta) * template.aspect
    y = r * np.sin(theta)
    c, s = math.cos(template.rotation), math.sin(template.rotation)
    x, y = x * c - y * s, x * s + y * c
    x, y = x - x[0], y - y[0]  # through home
    perimeter = float(np.hypot(np.diff(x), np.diff(y)).sum())
    scale = length_m / perimeter
    return x * scale, y * scale


# -- sessions --------------------------------------------------------------------------------------


@dataclass
class RunSpec:
    """How one synthetic run goes; enough to regenerate its records."""

    id: int
    start_utc: datetime
    kind: str
    distance_m: float
    template: str
    fitness: float
    """1.0 today; slower paces further back."""
    seed: int
    stop_at_m: float | None = None


def paces(fitness: float) -> dict[str, float]:
    """Target speeds in m/s for one fitness level (1.0 = now)."""

    def speed(pace_s: float) -> float:
        return 1000.0 / (pace_s * fitness)

    return {
        "easy": speed(332),
        "recovery": speed(360),
        "long": speed(338),
        "tempo": speed(262),
        "threshold": speed(248),
        "interval": speed(229),
        "hm": speed(277),
        "race10": speed(259),
        "jog": speed(410),
        "stride": speed(205),
    }


def _phases(spec: RunSpec) -> list[tuple[str, str, float, float]]:
    """(intensity, unit, amount, speed): the session as steps; unit ``m`` or ``s``."""
    v = paces(spec.fitness)
    d = spec.distance_m
    k = spec.kind
    if k == "threshold":
        reps = []
        for i in range(3):
            reps.append(("active", "s", 600.0, v["threshold"]))
            if i < 2:
                reps.append(("recovery", "s", 120.0, v["jog"]))
        return [("warmup", "m", 2000.0, v["easy"]), *reps, ("cooldown", "m", 1e9, v["easy"])]
    if k == "intervals":
        reps = []
        for i in range(6):
            reps.append(("active", "m", 800.0, v["interval"]))
            if i < 5:
                reps.append(("recovery", "s", 120.0, v["jog"]))
        return [("warmup", "m", 2500.0, v["easy"]), *reps, ("cooldown", "m", 1e9, v["easy"])]
    if k == "tempo":
        return [
            ("warmup", "m", 2000.0, v["easy"]),
            ("active", "m", 5000.0, v["tempo"]),
            ("cooldown", "m", 1e9, v["easy"]),
        ]
    if k == "easy_strides":
        strides = []
        for _ in range(6):
            strides += [("active", "s", 20.0, v["stride"]), ("recovery", "s", 45.0, 1.2)]
        return [("warmup", "m", d - 900.0, v["easy"]), *strides, ("cooldown", "m", 1e9, v["jog"])]
    if k == "long_fast":
        return [("active", "m", d - 5000.0, v["long"]), ("active", "m", 1e9, v["hm"])]
    if k == "race":
        speed = v["race10"] if d < 15000 else v["hm"]
        return [("active", "m", 1e9, speed)]
    if k == "recovery":
        return [("active", "m", 1e9, v["recovery"])]
    if k == "long":
        return [("active", "m", 1e9, v["long"])]
    return [("active", "m", 1e9, v["easy"])]


def simulate(spec: RunSpec) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Per-second records (the columns of ``activity_records``) and laps of one run."""
    rng = np.random.default_rng(spec.seed)
    x_r, y_r = route_xy(TEMPLATES[spec.template], spec.distance_m)
    seg = np.hypot(np.diff(x_r), np.diff(y_r))
    cum = np.r_[0.0, np.cumsum(seg)]
    alt_r = terrain(x_r, y_r)
    grade_r = np.gradient(alt_r, cum, edge_order=1)
    grade_r = np.convolve(grade_r, np.ones(9) / 9, mode="same")

    phases = _phases(spec)
    total = spec.distance_m
    grade_at = np.interp(np.arange(0, int(total) + 2, dtype=float), cum, grade_r).tolist()
    n_max = int(total / 0.6) + 600
    form = float(rng.normal(0, 3.0))  # how the heart rate runs on the day
    speed_noise = rng.normal(0, 0.035, n_max).tolist()
    hr_noise = rng.normal(0, 0.6, n_max).tolist()
    dist = 0.0
    hr = 96.0
    noise = 0.0
    rows_t, rows_d, rows_v, rows_hr = [], [], [], []
    laps: list[dict[str, Any]] = []
    t = 0
    stop_left = 0
    stopped = False
    for step_no, (intensity, unit, amount, target) in enumerate(phases, start=1):
        lap_start_t, lap_start_d = t, dist
        used = 0.0
        while dist < total and used < amount and t < n_max:
            g = grade_at[int(dist)]
            factor = 1 - 2.6 * g if g > 0 else 1 - 0.9 * g
            noise = 0.92 * noise + speed_noise[t]
            speed = max(0.6, target * min(1.08, factor) * (1 + noise))
            if spec.stop_at_m is not None and not stopped and dist >= spec.stop_at_m:
                stopped, stop_left = True, 34
            if stop_left > 0:
                speed = 0.0
                stop_left -= 1
            dist = min(total, dist + speed)
            # heart rate follows effort with a lag, and creeps up over long runs
            effort = 136 + form + 27 * (speed - 3.0) + 260 * max(g, -0.03)
            steady = (effort if speed > 0 else 118) + 0.045 * max(0, t - 1800) / 60
            tau = 22.0 if steady > hr else 38.0
            hr += (steady - hr) / tau + hr_noise[t]
            rows_t.append(t)
            rows_d.append(dist)
            rows_v.append(speed)
            rows_hr.append(hr)
            used += speed if unit == "m" else 1.0
            t += 1
        if t > lap_start_t:
            laps.append(
                {
                    "lap_no": len(laps) + 1,
                    "t0": lap_start_t,
                    "t1": t,
                    "d0": lap_start_d,
                    "d1": dist,
                    "intensity": intensity,
                    "wkt_step": step_no if len(phases) > 2 else None,
                }
            )
        if dist >= total:
            break

    t_arr = np.asarray(rows_t, dtype=float)
    d_arr = np.asarray(rows_d)
    x = np.interp(d_arr, cum, x_r) + rng.normal(0, 1.6, len(d_arr))
    y = np.interp(d_arr, cum, y_r) + rng.normal(0, 1.6, len(d_arr))
    alt = terrain(x, y) + np.cumsum(rng.normal(0, 0.03, len(d_arr)))
    hr_arr = np.clip(np.round(np.asarray(rows_hr)), 60, 205)
    speed_arr = np.asarray(rows_v)
    cad = np.where(
        speed_arr > 0.5, 158 + 8.5 * (speed_arr - 3.0) + rng.normal(0, 1.8, len(d_arr)), 0
    )
    ts = [spec.start_utc + timedelta(seconds=float(s)) for s in t_arr]
    df = pd.DataFrame(
        {
            "ts": ts,
            "elapsed_s": t_arr,
            "lat": np.round(LAT0 + y / M_PER_DEG, 7),
            "lon": np.round(LON0 + x / (M_PER_DEG * math.cos(math.radians(LAT0))), 7),
            "alt_m": np.round(alt, 1),
            "dist_m": np.round(d_arr, 1),
            "speed_mps": np.round(speed_arr, 3),
            "hr": hr_arr.astype(int),
            "cadence_spm": np.round(cad, 0),
            "power_w": None,
            "resp_rate": None,
            "temp_c": np.round(9.5 + 2.5 * t_arr / max(t_arr[-1], 1), 1),
        }
    )
    if spec.kind in ("easy", "long", "recovery", "race", "long_fast"):
        laps = _auto_laps(t_arr, d_arr)
    lap_rows = []
    for lap in laps:
        i0 = int(lap["t0"])
        i1 = min(int(lap["t1"]), len(df)) - 1
        duration = float(t_arr[i1] - t_arr[i0]) + 1
        dist_m = float(d_arr[i1] - (d_arr[i0 - 1] if i0 > 0 else 0.0))
        hr_part = hr_arr[i0 : i1 + 1]
        lap_rows.append(
            {
                "lap_no": lap["lap_no"],
                "start_utc": spec.start_utc + timedelta(seconds=float(t_arr[i0])),
                "duration_s": duration,
                "elapsed_s": duration,
                "dist_m": round(dist_m, 1),
                "avg_speed_mps": round(dist_m / duration, 3) if duration else None,
                "max_speed_mps": round(float(speed_arr[i0 : i1 + 1].max()), 3),
                "avg_hr": round(float(hr_part.mean()), 1),
                "max_hr": float(hr_part.max()),
                "avg_cadence_spm": round(float(cad[i0 : i1 + 1].mean()), 1),
                "avg_power_w": None,
                "ascent_m": None,
                "descent_m": None,
                "avg_temp_c": None,
                "intensity": lap["intensity"],
                "trigger": "distance" if lap.get("wkt_step") is None else "workout",
                "wkt_step": lap.get("wkt_step"),
            }
        )
    return df, lap_rows


def _auto_laps(t: np.ndarray, d: np.ndarray) -> list[dict[str, Any]]:
    out = []
    edges = list(range(1000, int(d[-1]) + 1, 1000))
    start = 0
    for edge in [*edges, None]:
        end = len(d) if edge is None else int(np.searchsorted(d, edge)) + 1
        if end - start > 5:
            out.append(
                {
                    "lap_no": len(out) + 1,
                    "t0": start,
                    "t1": min(end, len(d)),
                    "intensity": "active",
                    "wkt_step": None,
                }
            )
        start = end
    return out


# -- the whole athlete ----------------------------------------------------------------------------


@dataclass
class DemoData:
    scenario: str
    lang: str
    now: datetime
    activities: list[dict[str, Any]] = field(default_factory=list)
    specs: dict[int, RunSpec] = field(default_factory=dict)
    wellness: dict[date, dict[str, Any]] = field(default_factory=dict)
    levels: dict[date, list[dict[str, Any]]] = field(default_factory=dict)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    races: list[dict[str, Any]] = field(default_factory=list)
    segments: list[SegmentDef] = field(default_factory=list)
    efforts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def today(self) -> date:
        return self.now.date()


def _week_volume(monday: date, rng: random.Random) -> float:
    """Planned kilometres of a week: a slow build over two years, cutbacks every fourth week,
    a break in late summer 2029 and a block towards the half marathon in May 2030."""
    days = (monday - HISTORY_START).days
    base = 31 + 14 * min(days / 730, 1.0) + 14 * max(0.0, min((days - 770) / 70, 1.0))
    if date(2029, 8, 13) <= monday <= date(2029, 8, 26):
        return 0.0  # a calf niggle: two weeks off
    if date(2029, 8, 27) <= monday <= date(2029, 9, 9):
        base *= 0.55
    if (days // 7 + 1) % 4 == 3:
        base *= 0.72
    if monday == date(2030, 3, 11):  # the week of the 10K
        base *= 0.75
    return base * rng.uniform(0.9, 1.08)


def _fitness(day: date) -> float:
    """Pace factor (1.0 = today): about 6 % slower two years ago."""
    days = (DEMO_TODAY - day).days
    return (
        1.0
        + 0.06 * min(max(days, 0) / 830, 1.0)
        + (0.03 if date(2029, 8, 13) <= day <= date(2029, 9, 30) else 0.0)
    )


def _week_sessions(monday: date, volume: float, rng: random.Random) -> list[tuple[int, str, float]]:
    """(weekday, kind, km) of one week."""
    if volume <= 0:
        return []
    long_km = min(24.0, max(12.0, round(volume * 0.34)))
    quality = "threshold" if (monday.isocalendar().week % 2) else "intervals"
    if monday >= date(2030, 2, 18) and monday.isocalendar().week % 3 == 0:
        quality = "tempo"
    q_km = 11.0 if quality == "threshold" else 10.0 if quality == "intervals" else 10.5
    rest = max(volume - long_km - q_km, 14.0)
    weights = {2: 0.36, 3: 0.33, 5: 0.31} if volume < 62 else {2: 0.31, 3: 0.3, 4: 0.15, 5: 0.24}
    easy_days = list(weights)
    parts = [rest * weights[wd] * rng.uniform(0.9, 1.1) for wd in easy_days]
    sessions = [(1, quality, q_km)]
    for wd, km in zip(easy_days, parts, strict=True):
        kind = "easy_strides" if wd == 3 else ("recovery" if wd == 4 else "easy")
        sessions.append((wd, kind, max(5.0, round(km, 1))))
    long_kind = "long_fast" if monday >= date(2030, 3, 25) and monday.toordinal() % 2 else "long"
    sessions.append((6, long_kind, long_km))
    return sessions


TEMPLATE_FOR = {
    "threshold": "river",
    "intervals": "park",
    "tempo": "river",
    "easy": None,
    "easy_strides": "town",
    "recovery": "town",
    "long": "ridge",
    "long_fast": "river",
    "race": "town",
}


def _activity_row(
    spec: RunSpec, name: str, *, summary: dict[str, Any] | None, rng: random.Random
) -> dict[str, Any]:
    start_local = spec.start_utc.replace(tzinfo=None)
    if summary is None:
        v = paces(spec.fitness)
        speed = {
            "threshold": v["easy"] * 1.12,
            "intervals": v["easy"] * 1.1,
            "tempo": v["easy"] * 1.13,
            "long": v["long"],
            "long_fast": v["long"] * 1.04,
            "race": v["race10"],
            "recovery": v["recovery"],
        }.get(spec.kind, v["easy"])
        moving = spec.distance_m / speed * rng.uniform(0.98, 1.03)
        hilly = {"hills": 16, "ridge": 13, "town": 9, "river": 5, "park": 4}[spec.template]
        avg_hr = {
            "threshold": 152, "intervals": 150, "tempo": 154, "long": 141, "long_fast": 146,
            "race": 171, "recovery": 128,
        }.get(spec.kind, 137) + rng.uniform(-3, 3)  # fmt: skip
        summary = {
            "distance_m": float(round(spec.distance_m)),
            "duration_s": float(round(moving + rng.uniform(5, 25))),
            "moving_s": float(round(moving)),
            "avg_hr": round(avg_hr),
            "max_hr": round(
                avg_hr + {"intervals": 22, "threshold": 17, "race": 13}.get(spec.kind, 11)
            ),
            "elev_gain_m": round(spec.distance_m / 1000 * hilly * rng.uniform(0.8, 1.2)),
            "avg_cadence_spm": round(160 + rng.uniform(-3, 5)),
        }
        summary["elev_loss_m"] = summary["elev_gain_m"]
    duration = summary["duration_s"]
    hard = spec.kind in ("threshold", "intervals", "tempo", "race", "long_fast")
    return {
        "id": spec.id,
        "start_utc": spec.start_utc,
        "start_local": start_local,
        "local_date": start_local.date(),
        "type": "running",
        "is_run": True,
        "name": name,
        **summary,
        "elapsed_s": round(duration + rng.uniform(10, 60), 1),
        "avg_power_w": None,
        "vo2max": round(49.0 / spec.fitness, 1),
        "aerobic_te": round(3.4 if hard else 2.6 + rng.uniform(-0.3, 0.4), 1),
        "anaerobic_te": round(2.1 if spec.kind == "intervals" else 0.6 + rng.uniform(0, 0.6), 1),
        "training_load": round(duration / 60 * (1.9 if hard else 1.2)),
        "calories": round(spec.distance_m / 1000 * 68),
        "location": None,
        "has_records": False,
        "source": "demo",
    }


def _summary_from_records(df: pd.DataFrame) -> dict[str, Any]:
    p = prepare(
        df.rename(
            columns={"elapsed_s": "t", "dist_m": "dist", "speed_mps": "speed", "alt_m": "alt"}
        )
    )
    timer = float(p["timer"].iloc[-1])
    standing = float(p["still_s"].sum())
    up, down = ascent_descent(smooth_altitude(p))
    moving_cad = df.loc[df["speed_mps"] > 1.0, "cadence_spm"]
    return {
        "distance_m": float(round(df["dist_m"].iloc[-1])),
        "duration_s": float(round(timer + 1)),
        "moving_s": float(round(timer + 1 - standing)),
        "avg_hr": round(float(df["hr"].mean())),
        "max_hr": float(df["hr"].max()),
        "elev_gain_m": round(up),
        "elev_loss_m": round(down),
        "avg_cadence_spm": round(float(moving_cad.mean())) if len(moving_cad) else None,
    }


def _start_time(day: date, kind: str, rng: random.Random) -> datetime:
    if kind in ("long", "long_fast"):
        clock = time(8, rng.randrange(10, 50))
    elif kind == "race":
        clock = time(9, 30)
    elif day.weekday() == 5:
        clock = time(9, rng.randrange(0, 50))
    else:
        clock = (
            time(6, rng.randrange(5, 55)) if rng.random() < 0.75 else time(18, rng.randrange(0, 50))
        )
    return datetime.combine(day, clock, tzinfo=UTC)


def _build_runs(
    data: DemoData, rng: random.Random
) -> list[tuple[date, str, float, int | None, str]]:
    """Fill ``data.activities`` and ``data.specs``; returns the schedule of the last weeks
    (day, kind, km, activity id or None, status) for the plan."""
    names = NAMES[data.lang]
    today = data.today
    next_id = 7_000_001
    schedule = []
    monday = HISTORY_START
    this_monday = today - timedelta(days=today.weekday())
    while monday <= this_monday + timedelta(days=7):
        volume = _week_volume(monday, rng)
        for wd, kind, km in _week_sessions(monday, volume, rng):
            day = monday + timedelta(days=wd)
            if monday == date(2030, 3, 11) and wd == 6:
                kind, km = "race", 10.0
            if monday == date(2030, 3, 11) and wd == 5:
                kind, km = "easy_strides", 6.0
            this_week = monday == this_monday
            if this_week and wd == 1:
                kind = "threshold"
            if this_week and wd == 6:
                kind = "long_fast"
            skipped = rng.random() < 0.06 and day < today and kind != "race" and not this_week
            if day > today or (day == today and data.now.hour < 17):
                schedule.append((day, kind, km, None, "planned"))
                continue
            if skipped:
                schedule.append((day, kind, km, None, "skipped"))
                continue
            template = TEMPLATE_FOR[kind] or rng.choice(["river", "hills", "town", "hills"])
            if kind in ("long", "long_fast") and rng.random() < 0.4:
                template = "hills"
            spec = RunSpec(
                id=next_id,
                start_utc=_start_time(day, kind, rng),
                kind=kind,
                distance_m=km * 1000 * rng.uniform(0.99, 1.03) if kind != "race" else 10_050.0,
                template=template,
                fitness=_fitness(day) * (0.975 if kind == "race" else 1.0),
                seed=SEED + next_id,
                stop_at_m=km * 1000 * 0.43 if rng.random() < 0.3 and kind != "race" else None,
            )
            next_id += 1
            with_records = (today - day).days < RECORDS_DAYS
            summary = None
            if with_records:
                df, _ = simulate(spec)
                summary = _summary_from_records(df)
            name = names["race_10k"] if kind == "race" else names[kind]
            row = _activity_row(spec, name, summary=summary, rng=rng)
            row["has_records"] = with_records
            data.activities.append(row)
            data.specs[spec.id] = spec
            schedule.append((day, kind, km, spec.id, "done"))
            if kind in ("easy", "easy_strides") and day.weekday() == 5 and rng.random() < 0.25:
                data.activities.append(_other_activity(next_id, day, names["cycling"], rng))
                next_id += 1
        monday += timedelta(days=7)
    data.activities.sort(key=lambda a: a["start_utc"])
    return schedule


def _other_activity(activity_id: int, day: date, name: str, rng: random.Random) -> dict[str, Any]:
    start = datetime.combine(day, time(16, rng.randrange(0, 59)), tzinfo=UTC)
    duration = rng.uniform(3000, 5400)
    return {
        "id": activity_id,
        "start_utc": start,
        "start_local": start.replace(tzinfo=None),
        "local_date": day,
        "type": "road_biking",
        "is_run": False,
        "name": name,
        "distance_m": float(round(duration * 7.1)),
        "duration_s": float(round(duration)),
        "moving_s": float(round(duration - 120)),
        "elapsed_s": round(duration + 300, 1),
        "avg_hr": 124.0,
        "max_hr": 151.0,
        "elev_gain_m": 240.0,
        "elev_loss_m": 238.0,
        "avg_cadence_spm": None,
        "avg_power_w": None,
        "vo2max": None,
        "aerobic_te": 2.4,
        "anaerobic_te": 0.3,
        "training_load": 60.0,
        "calories": 700.0,
        "location": None,
        "has_records": False,
        "source": "demo",
    }


def _build_plan(data: DemoData, schedule: list[tuple[date, str, float, int | None, str]]) -> None:
    """Planned workouts from three weeks back to the end of next week, with rest days."""
    texts = PLAN_TEXT[data.lang]
    names = NAMES[data.lang]
    today = data.today
    first = today - timedelta(days=today.weekday() + 21)
    last = today - timedelta(days=today.weekday()) + timedelta(days=13)
    by_day = {d: (k, km, aid, st) for d, k, km, aid, st in schedule}
    created = datetime.combine(first - timedelta(days=2), time(19, 0), tzinfo=UTC)
    v = paces(1.0)
    pace_text = {
        "threshold": _pace_range(1000 / v["threshold"]),
        "intervals": _pace_range(1000 / v["interval"]),
        "tempo": _pace_range(1000 / v["tempo"]),
        "long_fast": _pace_range(1000 / v["hm"]),
        "easy": "5:20-5:45",
        "easy_strides": "5:20-5:45",
        "long": "5:25-5:50",
        "recovery": "5:50-6:20",
    }
    day = first
    wid = 1
    while day <= last:
        kind, km, aid, status = by_day.get(day, ("rest", 0.0, None, "planned"))
        if kind == "race":
            day += timedelta(days=1)
            continue
        if kind == "rest" and day.weekday() != 0:
            day += timedelta(days=1)
            continue
        if kind == "rest" and day < today:
            status = "done"
        description, hr = texts.get(kind, texts["easy"])
        this_week = today - timedelta(days=today.weekday()) <= day <= today
        if this_week and kind in LONG_DESCRIPTIONS[data.lang]:
            # the coach wrote at length for today's session and for Tuesday's blocks
            description = LONG_DESCRIPTIONS[data.lang][kind]
        target: dict[str, Any] = {}
        if kind != "rest":
            target["distance_km"] = round(km, 1)
            target["pace"] = pace_text.get(kind, "5:20-5:45")
            if hr:
                target["hr"] = hr
        data.workouts.append(
            {
                "id": wid,
                "date": day,
                "title": names.get(kind, kind),
                "kind": PLAN_KIND.get(kind, "other"),
                "description": description,
                "target": target,
                "status": status,
                "activity_id": aid,
                "created_at": created,
                "updated_at": created,
            }
        )
        wid += 1
        day += timedelta(days=1)


def _pace_range(seconds: float) -> str:
    lo, hi = seconds - 5, seconds + 5
    return f"{int(lo // 60)}:{int(lo % 60):02d}-{int(hi // 60)}:{int(hi % 60):02d}"


def _build_nights(data: DemoData, rng: random.Random, first: date) -> None:
    hard_days = {
        a["local_date"]
        for a in data.activities
        if data.specs.get(a["id"])
        and data.specs[a["id"]].kind
        in ("threshold", "intervals", "long", "long_fast", "race", "tempo")
    }
    base = 62.0
    day = first
    while day <= data.today:
        base += rng.gauss(0, 0.35) + (62.5 - base) * 0.04
        sick = date(2030, 2, 3) <= day <= date(2030, 2, 6)
        after_hard = (day - timedelta(days=1)) in hard_days
        hrv = base + rng.gauss(0, 4.2) - (5.5 if after_hard else 0) - (13 if sick else 0)
        rhr = 47 + rng.gauss(0, 0.9) + (1.6 if after_hard else 0) + (5 if sick else 0)
        bed = datetime.combine(day - timedelta(days=1), time(22, 30), tzinfo=UTC) + timedelta(
            minutes=rng.gauss(18, 22)
        )
        length_min = rng.gauss(452, 32) - (35 if sick else 0)
        levels = _night_levels(bed, length_min, rng)
        totals = {"deep": 0, "light": 0, "rem": 0, "awake": 0}
        for lv in levels:
            totals[lv["stage"]] += round((lv["end_utc"] - lv["start_utc"]).total_seconds())
        sleep_s = totals["deep"] + totals["light"] + totals["rem"]
        score = int(
            max(48, min(95, 42 + sleep_s / 3600 * 6.4 - totals["awake"] / 300 + rng.gauss(0, 3)))
        )
        end = levels[-1]["end_utc"]
        week = [w["hrv_night_avg"] for d, w in data.wellness.items() if (day - d).days < 7]
        weekly = (sum(week) + hrv) / (len(week) + 1)
        low, high = round(base - 8), round(base + 8)
        status = "BALANCED" if low <= weekly <= high else ("UNBALANCED" if weekly > high else "LOW")
        data.wellness[day] = {
            "wake_date": day,
            "sleep_score": score,
            "sleep_s": sleep_s,
            "deep_s": totals["deep"],
            "light_s": totals["light"],
            "rem_s": totals["rem"],
            "awake_s": totals["awake"],
            "sleep_start_utc": levels[0]["start_utc"],
            "sleep_end_utc": end,
            "hrv_night_avg": round(hrv),
            "hrv_weekly_avg": round(weekly),
            "hrv_status": status,
            "hrv_balanced_low": float(low),
            "hrv_balanced_high": float(high),
            "resting_hr": float(round(rhr)),
            "steps": int(rng.gauss(11500, 2600)) if day < data.today else int(rng.gauss(900, 200)),
            "stress_avg": float(round(rng.gauss(27, 5))),
            "body_battery_charged": int(min(100, max(20, rng.gauss(68, 10)))),
            "sleep_synced_at": end + timedelta(minutes=rng.uniform(12, 40)),
        }
        data.levels[day] = levels
        day += timedelta(days=1)


def _night_levels(bed: datetime, length_min: float, rng: random.Random) -> list[dict[str, Any]]:
    """Sleep stages in cycles of about 90 minutes: more deep sleep early, more REM late."""
    out: list[dict[str, Any]] = []
    t = bed
    end = bed + timedelta(minutes=length_min)
    cycle = 0

    def add(stage: str, minutes: float) -> None:
        nonlocal t
        minutes = max(2.0, minutes)
        stop = min(end, t + timedelta(minutes=minutes))
        if stop <= t:
            return
        if out and out[-1]["stage"] == stage:
            out[-1]["end_utc"] = stop
        else:
            out.append({"start_utc": t, "end_utc": stop, "stage": stage})
        t = stop

    add("light", rng.uniform(8, 18))
    while t < end:
        late = cycle / 5
        add("deep", rng.gauss(38 - 26 * late, 6))
        add("light", rng.gauss(26 + 10 * late, 6))
        add("rem", rng.gauss(12 + 22 * late, 5))
        if rng.random() < 0.45:
            add("awake", rng.uniform(2, 7))
        add("light", rng.gauss(10, 4))
        cycle += 1
    if out[-1]["stage"] != "awake":
        out.append({"start_utc": t, "end_utc": t + timedelta(minutes=4), "stage": "awake"})
    level_raw = {"deep": 0, "light": 1, "rem": 2, "awake": 3}
    for lv in out:
        lv["start_utc"] = lv["start_utc"].replace(second=0, microsecond=0)
        lv["end_utc"] = lv["end_utc"].replace(second=0, microsecond=0)
        lv["level_raw"] = level_raw[lv["stage"]]
    return [lv for lv in out if lv["end_utc"] > lv["start_utc"]]


def _build_races(data: DemoData) -> None:
    names = NAMES[data.lang]
    race_run = next(
        (a for a in data.activities if a["is_run"] and a["local_date"] == date(2030, 3, 17)), None
    )
    result = None
    if race_run is not None:
        t = race_run["duration_s"]
        result = {
            "time_s": round(t),
            "time": _clock(t),
            "source": "official",
            "place": "41 / 612",
            "watch": {
                "activity_id": race_run["id"],
                "distance_km": round(race_run["distance_m"] / 1000, 2),
                "time_s": race_run["duration_s"],
                "moving_s": race_run["moving_s"],
                "elapsed_s": race_run["elapsed_s"],
                "avg_hr": race_run["avg_hr"],
                "elev_gain_m": race_run["elev_gain_m"],
            },
        }
    data.races = [
        {
            "id": 1,
            "date": date(2030, 3, 17),
            "name": names["race_10k"],
            "distance_km": 10.0,
            "elev_m": 35.0,
            "location": None,
            "start_time_local": time(9, 30),
            "priority": "B",
            "goal": "sub 43:30",
            "status": "done",
            "activity_id": race_run["id"] if race_run else None,
            "result": result,
            "url": None,
            "notes": None,
        },
        {
            "id": 2,
            "date": date(2030, 5, 26),
            "name": names["race_half"],
            "distance_km": 21.1,
            "elev_m": 60.0,
            "location": None,
            "start_time_local": time(9, 0),
            "priority": "A",
            "goal": "1:37:30",
            "status": "planned",
            "activity_id": None,
            "result": None,
            "url": None,
            "notes": None,
        },
    ]


def _clock(seconds: float) -> str:
    total = round(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _build_segments(data: DemoData, rng: random.Random) -> None:
    names = NAMES[data.lang]
    lon_scale = M_PER_DEG * math.cos(math.radians(LAT0))
    data.segments = [
        SegmentDef(
            key="quarry",
            name=names["seg_quarry"],
            start_lat=LAT0 + 700 / M_PER_DEG,
            start_lon=LON0 + 1100 / lon_scale,
            end_lat=LAT0 + 1300 / M_PER_DEG,
            end_lon=LON0 + 1900 / lon_scale,
            tol_m=30,
            dist_min_m=1200,
            dist_max_m=1600,
        ),
        SegmentDef(
            key="ridge",
            name=names["seg_ridge"],
            start_lat=LAT0 - 900 / M_PER_DEG,
            start_lon=LON0 - 1300 / lon_scale,
            end_lat=LAT0 + 600 / M_PER_DEG,
            end_lon=LON0 - 2500 / lon_scale,
            tol_m=30,
            dist_min_m=1900,
            dist_max_m=2300,
        ),
    ]
    shapes = {"quarry": (1380.0, 71.0, 492.0), "ridge": (2090.0, 47.0, 655.0)}
    for seg in data.segments:
        dist_m, gain, base_s = shapes[seg.key]
        efforts = []
        for a in data.activities:
            spec = data.specs.get(a["id"])
            if spec is None:
                continue
            on_route = spec.template == "hills" if seg.key == "quarry" else spec.template == "ridge"
            if not on_route or rng.random() < 0.55:  # not every run on the route takes the climb
                continue
            push = rng.random() < 0.18
            dur = base_s * spec.fitness * rng.uniform(0.97, 1.05) * (0.9 if push else 1.0)
            hr = (149 if not push else 164) + rng.gauss(0, 3)
            efforts.append(
                {
                    "activity_id": a["id"],
                    "start_utc": a["start_utc"] + timedelta(seconds=rng.uniform(900, 2400)),
                    "duration_s": round(dur, 1),
                    "dist_m": round(dist_m * rng.uniform(0.99, 1.01), 1),
                    "gain_m": round(gain + rng.gauss(0, 1.2), 1),
                    "avg_hr": round(hr, 1),
                    "max_hr": round(hr + rng.uniform(5, 11), 1),
                    "hr_last60": round(hr + rng.uniform(3, 8), 1),
                    "pace_s_per_km": round(dur / dist_m * 1000, 1),
                }
            )
        data.efforts[seg.key] = efforts


BRIEFING = {
    "en": (
        "Good morning! You slept {sleep} and your HRV ({hrv} ms) sits right in your usual"
        " range, so Tuesday's threshold session has been absorbed well.\n\n"
        "**Today:** easy {km} km at 5:20–5:45/km with 6 × 20 s strides at the end, heart rate"
        " under 145. Keep it genuinely relaxed: Sunday's long run with the last 5 km at"
        " half-marathon pace is the key session of the week.\n\n"
        "Five and a half weeks to the half marathon, and you are on track."
    ),
    "de": (
        "Guten Morgen! Du hast {sleep} geschlafen, und deine HRV ({hrv} ms) liegt mitten in"
        " deinem üblichen Bereich: die Schwelleneinheit vom Dienstag ist gut verdaut.\n\n"
        "**Heute:** locker {km} km bei 5:20–5:45/km, am Ende 6 × 20 s Steigerungen, Puls unter"
        " 145. Wirklich locker bleiben: der lange Lauf am Sonntag mit den letzten 5 km im"
        " Halbmarathontempo ist die wichtigste Einheit der Woche.\n\n"
        "Noch fünfeinhalb Wochen bis zum Halbmarathon, und du bist auf Kurs."
    ),
}
DEBRIEF = {
    "en": (
        "Nice and easy yesterday: {km} km at {pace}/km with the heart rate where it belongs."
        " And Tuesday's three threshold blocks were clean:\n\n"
        "| Block | Pace | HR |\n|---|---:|---:|\n{rows}\n\n"
        "Thursday stays easy, and Sunday's long run with the fast finish is the key session."
    ),
    "de": (
        "Schön locker gestern: {km} km bei {pace}/km, der Puls genau da, wo er hingehört."
        " Und die drei Schwellenblöcke vom Dienstag waren sauber:\n\n"
        "| Block | Pace | Puls |\n|---|---:|---:|\n{rows}\n\n"
        "Der Donnerstag bleibt locker, die wichtigste Einheit ist der lange Lauf am Sonntag"
        " mit der schnellen Endphase."
    ),
}
LONG_DESCRIPTIONS = {
    "en": {
        "easy_strides": (
            "Easy on the river loop, conversational the whole way: if you cannot talk in full"
            " sentences, slow down. Keep the heart rate under 145; on the rise after the bridge"
            " let it drift to 150 at most and walk a few steps if it goes higher. It is meant to"
            " be mild and windy this morning, so start into the wind and come home with it at"
            " your back.\n\n"
            "Afterwards, on the flat stretch by the boathouse, 6 × 20 s strides: smooth and"
            " quick, about 5K effort, never a sprint, with a full walk back in between (at least"
            " a minute). Leave the strides out if the calves still feel Tuesday or the path is"
            " wet and slippery.\n\n"
            "Wear the light trainers, not the racing shoes: they stay in the cupboard until the"
            " long run on Sunday. Stop early and walk home if anything twinges; one easy day too"
            " few costs nothing, a niggle five weeks before the half marathon costs a lot. Eat"
            " something within half an hour afterwards."
        ),
        "threshold": (
            "2 km warm-up at an easy jog, then three blocks of 10 minutes at threshold with"
            " 2 minutes of easy jogging between them. The blocks should feel comfortably hard:"
            " a few words are possible, a conversation is not. Do not chase the pace on the"
            " rise in the second block; hold the effort and let the pace drop a little. The"
            " heart rate should settle around 160 to 166 and not climb past 170.\n\n"
            "If the first block already feels like a race, turn the session into an easy run"
            " and we move the blocks to Thursday. Cool down with 2 km easy, then stretch calves"
            " and hips. Everyday trainers; drink before you leave, it will be warm in the"
            " evening."
        ),
    },
    "de": {
        "easy_strides": (
            "Locker auf der Flussrunde, die ganze Zeit im Gesprächstempo: wenn ganze Sätze"
            " nicht mehr gehen, langsamer. Puls unter 145; am Anstieg nach der Brücke darf er"
            " bis 150 steigen, darüber ein paar Schritte gehen. Heute Morgen soll es mild und"
            " windig sein, also gegen den Wind los und mit Rückenwind zurück.\n\n"
            "Danach auf dem flachen Stück am Bootshaus 6 × 20 s Steigerungen: locker und"
            " schnell, etwa 5-km-Tempo, nie ein Sprint, dazwischen ganz zurückgehen (mindestens"
            " eine Minute). Die Steigerungen weglassen, wenn die Waden den Dienstag noch spüren"
            " oder der Weg nass und rutschig ist.\n\n"
            "Die leichten Trainingsschuhe, nicht die Wettkampfschuhe: die bleiben bis zum langen"
            " Lauf am Sonntag im Schrank. Früher aufhören und nach Hause gehen, wenn irgendetwas"
            " zwickt; ein lockerer Tag zu wenig kostet nichts, eine Zerrung fünf Wochen vor dem"
            " Halbmarathon viel. Innerhalb einer halben Stunde danach etwas essen."
        ),
        "threshold": (
            "2 km locker einlaufen, dann drei Blöcke à 10 Minuten an der Schwelle mit je"
            " 2 Minuten lockerem Traben dazwischen. Die Blöcke sollen sich angenehm hart"
            " anfühlen: ein paar Worte gehen, ein Gespräch nicht. Am Anstieg im zweiten Block"
            " nicht der Pace hinterherlaufen, sondern die Anstrengung halten und die Pace etwas"
            " fallen lassen. Der Puls sollte sich bei 160 bis 166 einpendeln und nicht über 170"
            " steigen.\n\n"
            "Fühlt sich schon der erste Block wie ein Wettkampf an, wird daraus ein lockerer"
            " Lauf, und die Blöcke wandern auf Donnerstag. 2 km locker auslaufen, dann Waden und"
            " Hüfte dehnen. Normale Trainingsschuhe; vor dem Start trinken, abends wird es warm."
        ),
    },
}


def _laps_frame(
    laps: list[dict[str, Any]], activity_id: int, first_ts: pd.Timestamp | None
) -> pd.DataFrame:
    """Laps shaped like :func:`thresy.analysis.data.load_laps` returns them."""
    df = pd.DataFrame(laps)
    if df.empty:
        return df
    df.insert(0, "activity_id", activity_id)
    for col in df.columns:
        if col not in ("start_utc", "intensity", "trigger"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["start_utc"] = pd.to_datetime(df["start_utc"], utc=True)
    if first_ts is not None:
        df["t_start"] = (df["start_utc"] - pd.Timestamp(first_ts)).dt.total_seconds()
        nxt = df["t_start"].shift(-1)
        own = df["t_start"] + df["elapsed_s"].fillna(df["duration_s"])
        df["t_end"] = nxt.where(nxt.notna(), own)
    return df


def _block_rows(data: DemoData, row: dict[str, Any], lang: str) -> str:
    """The reps of one structured run as Markdown table rows, as the coach reports them
    (from the same analysis as the run's page)."""
    from thresy.analysis import intervals
    from thresy.analysis.data import RECORD_COLUMNS

    raw, laps = simulate(data.specs[row["id"]])
    df = prepare(raw.rename(columns=RECORD_COLUMNS))
    result = intervals.analyze(_activity(row), df, _laps_frame(laps, row["id"], df["ts"].iloc[0]))
    lines = []
    for rep in result["reps"]:
        pace = round(rep["pace_s_per_km"])
        lines.append(f"| {rep['n']} | {pace // 60}:{pace % 60:02d} | {round(rep['avg_hr'])} |")
    return "\n".join(lines)


def _build_messages(data: DemoData) -> None:
    night = data.wellness.get(data.today)
    today_plan = next((w for w in data.workouts if w["date"] == data.today), None)
    km = today_plan["target"].get("distance_km", 10) if today_plan else 10
    sleep_s = night["sleep_s"] if night else 0
    minutes = round(sleep_s / 60)
    sleep = f"{minutes // 60} h {minutes % 60:02d} min"
    km_text = f"{km:g}".replace(".", ",") if data.lang == "de" else f"{km:g}"
    yesterday = [a for a in data.activities if a["local_date"] == data.today - timedelta(days=1)]
    monday = data.today - timedelta(days=data.today.weekday())
    blocks = next(
        (
            a
            for a in data.activities
            if monday <= a["local_date"] < data.today
            and a["id"] in data.specs
            and data.specs[a["id"]].kind == "threshold"
        ),
        None,
    )
    if yesterday and blocks is not None:
        a = yesterday[-1]
        pace = round((a["moving_s"] or a["duration_s"]) / (a["distance_m"] / 1000))
        dkm = f"{a['distance_m'] / 1000:.1f}"
        data.messages.append(
            {
                "ts": a["start_utc"] + timedelta(seconds=a["elapsed_s"] + 600),
                "text": DEBRIEF[data.lang].format(
                    km=dkm.replace(".", ",") if data.lang == "de" else dkm,
                    pace=f"{pace // 60}:{pace % 60:02d}",
                    rows=_block_rows(data, blocks, data.lang),
                ),
                "trigger": "post_run",
            }
        )
    data.messages.append(
        {
            "ts": datetime.combine(data.today, time(7, 12), tzinfo=UTC),
            "text": BRIEFING[data.lang].format(
                sleep=sleep, hrv=night["hrv_night_avg"] if night else "–", km=km_text
            ),
            "trigger": "morning_briefing",
        }
    )


def _build_state(data: DemoData, *, new: bool = False) -> None:
    now = data.now
    data.state = {
        "sync_status": {
            "last_attempt_at": (now - timedelta(minutes=4)).isoformat(),
            "last_success_at": (now - timedelta(minutes=4)).isoformat(),
            "consecutive_failures": 0,
        },
        "garmin_auth": {"status": "ok", "last_ok_at": (now - timedelta(minutes=4)).isoformat()},
        "garmin_rate_limit": {},
        "backup_status": {}
        if new
        else {
            "last_success_at": datetime.combine(now.date(), time(3, 31), tzinfo=UTC).isoformat(),
            "last_attempt_at": datetime.combine(now.date(), time(3, 30), tzinfo=UTC).isoformat(),
            "failures_in_row": 0,
            "restore_check": {
                "at": (now - timedelta(days=3, hours=4)).isoformat(),
                "ok": True,
                "source": "scheduled",
                "detail": "restored into a scratch database",
            },
        },
    }


def _build_new(data: DemoData, rng: random.Random) -> None:
    """An athlete who connected Garmin four days ago: three runs, two nights, no plan."""
    names = NAMES[data.lang]
    today = data.today
    for n, (days_back, kind, km) in enumerate(
        ((9, "easy", 6.2), (6, "easy", 8.1), (2, "long", 12.4))
    ):
        day = today - timedelta(days=days_back)
        spec = RunSpec(
            id=7_100_001 + n,
            start_utc=_start_time(day, kind, rng),
            kind=kind,
            distance_m=km * 1000,
            template=["town", "river", "hills"][n],
            fitness=1.04,
            seed=SEED + 7_100_001 + n,
        )
        df, _ = simulate(spec)
        row = _activity_row(
            spec,
            names["first_run"] if n == 0 else names[kind],
            summary=_summary_from_records(df),
            rng=rng,
        )
        row["has_records"] = True
        data.activities.append(row)
        data.specs[spec.id] = spec
    _build_nights(data, rng, today - timedelta(days=3))
    # last night has not synced yet
    data.wellness.pop(today, None)
    data.levels.pop(today, None)
    _build_state(data, new=True)


@lru_cache(maxsize=4)
def build(scenario: str = "full", lang: str = "en") -> DemoData:
    """The synthetic athlete (cached; treat the result as read-only)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown demo scenario {scenario!r}; use one of: {', '.join(SCENARIOS)}")
    lang = lang if lang in NAMES else "en"
    rng = random.Random(SEED)
    data = DemoData(scenario=scenario, lang=lang, now=DEMO_NOW, levels={})
    if scenario == "new":
        _build_new(data, rng)
        return data
    schedule = _build_runs(data, rng)
    _build_plan(data, schedule)
    _build_nights(data, rng, NIGHTS_FROM)
    _build_races(data)
    _build_segments(data, rng)
    _build_messages(data)
    _build_state(data)
    if scenario == "trouble":
        _make_trouble(data)
    return data


def _make_trouble(data: DemoData) -> None:
    """Everything that should catch the owner's eye: the Garmin login has expired a day ago
    (so last night never arrived), and the last two backups failed."""
    now = data.now
    data.wellness.pop(data.today, None)
    data.levels.pop(data.today, None)
    data.messages = [m for m in data.messages if m["trigger"] != "morning_briefing"]
    last_ok = (now - timedelta(hours=26)).isoformat()
    data.state["sync_status"] = {
        "last_attempt_at": (now - timedelta(minutes=6)).isoformat(),
        "last_success_at": last_ok,
        "consecutive_failures": 31,
        "last_error": "Garmin login needed",
        "last_error_kind": "auth",
        "last_error_at": (now - timedelta(minutes=6)).isoformat(),
    }
    data.state["garmin_auth"] = {"status": "login_failed", "reason": "the session expired"}
    data.state["backup_status"] = {
        **data.state["backup_status"],
        "last_success_at": (now - timedelta(days=2, hours=4)).isoformat(),
        "failures_in_row": 2,
        "last_error": "upload refused: the bucket's quota is exceeded",
    }


# -- the source ------------------------------------------------------------------------------------

_ACTIVITY_FIELDS = (
    "id", "start_utc", "start_local", "local_date", "type", "is_run", "name", "distance_m",
    "duration_s", "moving_s", "avg_hr", "max_hr", "elev_gain_m", "elev_loss_m",
    "avg_cadence_spm", "avg_power_w", "has_records",
)  # fmt: skip


def _activity(row: dict[str, Any]) -> Activity:
    return Activity(**{k: row[k] for k in _ACTIVITY_FIELDS})


class DemoSource:
    """:class:`thresy.web.source.Source` over :func:`build`."""

    demo = True

    def __init__(self, scenario: str = "full", lang: str = "en") -> None:
        self.data = build(scenario, lang)
        self.zone = DEMO_ZONE
        self._by_id = {a["id"]: a for a in self.data.activities}

    def now(self) -> datetime:
        return self.data.now

    def today(self) -> date:
        return self.data.today

    # wellness ------------------------------------------------------------------------------------

    def night(self, wake_date: date) -> dict[str, Any]:
        row = self.data.wellness.get(wake_date)
        levels = self.data.levels.get(wake_date, [])
        synced = bool(row and row["sleep_s"])
        out: dict[str, Any] = {
            "wake_date": wake_date,
            "synced": synced,
            "sleep_synced_at": row["sleep_synced_at"] if row else None,
        }
        if not synced:
            nights = [d for d, w in self.data.wellness.items() if w["sleep_s"]]
            out["latest_synced"] = max(nights) if nights else None
            return out
        assert row is not None

        def local(ts: datetime | None) -> datetime | None:
            return None if ts is None else ts.astimezone(UTC).replace(tzinfo=None)

        totals: dict[str, float] = {}
        for lv in levels:
            totals[lv["stage"]] = (
                totals.get(lv["stage"], 0.0) + (lv["end_utc"] - lv["start_utc"]).total_seconds()
            )
        out.update(
            sleep={
                "start_local": local(row["sleep_start_utc"]),
                "end_local": local(row["sleep_end_utc"]),
                "sleep_s": row["sleep_s"],
                "score": row["sleep_score"],
                "deep_s": row["deep_s"],
                "light_s": row["light_s"],
                "rem_s": row["rem_s"],
                "awake_s": row["awake_s"],
                "stages_from_levels_s": {k: round(v) for k, v in totals.items()},
            },
            garmin_hrv={
                "night_avg": row["hrv_night_avg"],
                "weekly_avg": row["hrv_weekly_avg"],
                "status": row["hrv_status"],
                "balanced_low": row["hrv_balanced_low"],
                "balanced_high": row["hrv_balanced_high"],
            },
            resting_hr=row["resting_hr"],
            levels=[
                {
                    "start_local": local(lv["start_utc"]),
                    "end_local": local(lv["end_utc"]),
                    "stage": lv["stage"],
                }
                for lv in levels
            ],
        )
        return out

    def wellness(self, end: date, days: int) -> dict[str, Any]:
        from thresy.analysis.wellness import COLUMNS

        start = end - timedelta(days=days - 1)
        daily = []
        for n in range(days):
            day = start + timedelta(days=n)
            r = dict(self.data.wellness.get(day, {"wake_date": day}))
            hrv, low, high = (
                r.get("hrv_night_avg"),
                r.get("hrv_balanced_low"),
                r.get("hrv_balanced_high"),
            )
            r["below_range"] = bool(hrv is not None and low is not None and hrv < low)
            r["above_range"] = bool(hrv is not None and high is not None and hrv > high)
            daily.append({k: r.get(k) for k in (*COLUMNS, "below_range", "above_range")})

        def mean(values: list[Any], digits: int = 1) -> float | None:
            vals = [float(v) for v in values if v is not None]
            return round(float(np.mean(vals)), digits) if vals else None

        def block(items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "hrv": mean([d["hrv_night_avg"] for d in items]),
                "resting_hr": mean([d["resting_hr"] for d in items]),
                "sleep_s": mean([d["sleep_s"] for d in items], 0),
                "sleep_score": mean([d["sleep_score"] for d in items]),
                "nights": sum(1 for d in items if d["sleep_s"]),
            }

        last7 = [d for d in daily if d["wake_date"] > end - timedelta(days=7)]
        prev7 = [
            d for d in daily if end - timedelta(days=14) < d["wake_date"] <= end - timedelta(days=7)
        ]
        latest = next((d for d in reversed(daily) if d["hrv_night_avg"] is not None), None)
        return {
            "from": start,
            "to": end,
            "days": days,
            "summary": {
                "last7": block(last7),
                "prev7": block(prev7),
                "period": block(daily),
                "nights_below_range": sum(d["below_range"] for d in daily),
                "nights_above_range": sum(d["above_range"] for d in daily),
                "days_without_sleep": sum(1 for d in daily if not d["sleep_s"]),
                "latest_hrv": latest,
            },
            "daily": daily,
        }

    # running -------------------------------------------------------------------------------------

    def _totals(self, start: date, end: date) -> dict[str, Any]:
        rows = [a for a in self.data.activities if a["is_run"] and start <= a["local_date"] <= end]
        distance = sum(a["distance_m"] or 0 for a in rows)
        longest = max((a["distance_m"] for a in rows if a["distance_m"]), default=None)
        return {
            "from": start,
            "to": end,
            "runs": len(rows),
            "distance_km": round(distance / 1000, 1),
            "time_s": round(sum((a["moving_s"] or a["duration_s"] or 0) for a in rows)),
            "ascent_m": round(sum(a["elev_gain_m"] or 0 for a in rows)),
            "longest_km": round(longest / 1000, 1) if longest else None,
        }

    def weeks(self, today: date, n_weeks: int) -> dict[str, Any]:
        monday = today - timedelta(days=today.weekday())
        out = []
        for k in range(n_weeks - 1, -1, -1):
            start = monday - timedelta(weeks=k)
            end = min(start + timedelta(days=6), today)
            out.append({"week": start.isocalendar().week, **self._totals(start, end)})
        try:
            same_day = today.replace(year=today.year - 1)
        except ValueError:
            same_day = today.replace(year=today.year - 1, day=28)
        return {
            "today": today,
            "runs_only": True,
            "weeks": out,
            "ytd": self._totals(date(today.year, 1, 1), today),
            "ytd_last_year": self._totals(date(today.year - 1, 1, 1), same_day),
        }

    def plan_week(self, monday: date, weeks: int, today: date) -> dict[str, Any]:
        last = monday + timedelta(days=7 * weeks - 1)
        workouts = [
            dict(w, target=dict(w["target"]))
            for w in self.data.workouts
            if monday <= w["date"] <= last
        ]
        workouts.sort(key=lambda w: (w["date"], w["id"]))
        acts = [a for a in self.data.activities if monday <= a["local_date"] <= last]
        linked: dict[int, list[int]] = {}
        for w in workouts:
            if w["activity_id"]:
                linked.setdefault(w["activity_id"], []).append(w["id"])
        days = []
        for offset in range((last - monday).days + 1):
            day = monday + timedelta(days=offset)
            items = []
            for a in acts:
                if a["local_date"] != day:
                    continue
                time_s = a["moving_s"] if a["moving_s"] is not None else a["duration_s"]
                item = {
                    "id": a["id"],
                    "local_date": a["local_date"],
                    "start_local": a["start_local"],
                    "type": a["type"],
                    "is_run": a["is_run"],
                    "name": a["name"],
                    "distance_m": a["distance_m"],
                    "time_s": time_s,
                    "avg_hr": a["avg_hr"],
                    "elev_gain_m": a["elev_gain_m"],
                }
                item["pace_s_per_km"] = (
                    time_s / (a["distance_m"] / 1000)
                    if a["is_run"] and a["distance_m"] and time_s
                    else None
                )
                item["linked_to"] = linked.get(a["id"], [])
                items.append(item)
            days.append(
                {
                    "date": day,
                    "workouts": [w for w in workouts if w["date"] == day],
                    "activities": items,
                }
            )
        counted = [w for w in workouts if w["status"] != "moved"]
        runs = [i for d in days for i in d["activities"] if i["is_run"]]
        all_acts = [i for d in days for i in d["activities"]]
        planned_km = sum(
            float(w["target"]["distance_km"])
            for w in counted
            if isinstance(w["target"].get("distance_km"), int | float)
        )
        totals = {
            "planned": len(counted),
            "done": sum(w["status"] == "done" for w in counted),
            "skipped": sum(w["status"] == "skipped" for w in counted),
            "moved": sum(w["status"] == "moved" for w in workouts),
            "open": sum(w["status"] == "planned" and w["date"] >= today for w in counted),
            "unmarked_past": sum(w["status"] == "planned" and w["date"] < today for w in counted),
            "planned_km": round(planned_km, 1),
            "runs": len(runs),
            "run_km": round(sum((a["distance_m"] or 0) for a in runs) / 1000, 1),
            "run_time_s": round(sum((a["time_s"] or 0) for a in runs)),
            "run_ascent_m": round(sum((a["elev_gain_m"] or 0) for a in runs)),
            "other_activities": len(all_acts) - len(runs),
        }
        return {"from": monday, "to": last, "today": today, "days": days, "totals": totals}

    def runs(
        self, *, since: date | None = None, until: date | None = None, limit: int | None = None
    ) -> list[Activity]:
        rows = [
            a
            for a in self.data.activities
            if a["is_run"]
            and (since is None or a["local_date"] >= since)
            and (until is None or a["local_date"] <= until)
        ]
        rows.sort(key=lambda a: (-a["start_utc"].timestamp(), a["id"]))
        if limit:
            rows = rows[:limit]
        return [_activity(a) for a in rows]

    def run_years(self) -> list[dict[str, Any]]:
        years: dict[int, dict[str, Any]] = {}
        for a in self.data.activities:
            if not a["is_run"]:
                continue
            y = years.setdefault(
                a["local_date"].year, {"year": a["local_date"].year, "runs": 0, "distance_m": 0.0}
            )
            y["runs"] += 1
            y["distance_m"] += a["distance_m"] or 0.0
        return sorted(years.values(), key=lambda y: -y["year"])

    def activity(self, activity_id: int) -> Activity | None:
        row = self._by_id.get(activity_id)
        return None if row is None else _activity(row)

    def activity_extra(self, activity_id: int) -> dict[str, Any]:
        row = self._by_id.get(activity_id)
        keys = ("aerobic_te", "anaerobic_te", "training_load", "calories", "vo2max")
        return {k: row[k] for k in keys} if row else {}

    def raw_records(self, activity_id: int) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        row = self._by_id.get(activity_id)
        spec = self.data.specs.get(activity_id)
        if row is None or spec is None or not row["has_records"]:
            return pd.DataFrame(), []
        return _simulated(spec.id, self.data.scenario, self.data.lang)

    def records(self, activity_id: int) -> pd.DataFrame:
        from thresy.analysis.data import RECORD_COLUMNS

        raw, _ = self.raw_records(activity_id)
        if raw.empty:
            return prepare(pd.DataFrame(columns=list(RECORD_COLUMNS.values())))
        return prepare(raw.rename(columns=RECORD_COLUMNS))

    def laps(self, activity_id: int, first_ts: pd.Timestamp | None) -> pd.DataFrame:
        _, laps = self.raw_records(activity_id)
        return _laps_frame(laps, activity_id, first_ts)

    def daily_distance(self, since: date, until: date) -> list[tuple[date, float]]:
        out: dict[date, float] = {}
        for a in self.data.activities:
            if a["is_run"] and since <= a["local_date"] <= until and (a["distance_m"] or 0) > 0:
                out[a["local_date"]] = out.get(a["local_date"], 0.0) + a["distance_m"]
        return sorted(out.items())

    # plan, races, segments ------------------------------------------------------------------------

    def races(self, today: date) -> list[dict[str, Any]]:
        return sorted((dict(r) for r in self.data.races), key=lambda r: (r["date"], r["id"]))

    def segments(self) -> list[SegmentDef]:
        return sorted(self.data.segments, key=lambda s: s.key)

    def segment_history(self, seg: SegmentDef) -> dict[str, Any]:
        from thresy.analysis.segments import with_derived

        rows = []
        for e in sorted(self.data.efforts.get(seg.key, []), key=lambda e: e["start_utc"]):
            activity = self.activity(e["activity_id"])
            row = with_derived(e)
            row["start_local"] = activity.local(e["start_utc"]) if activity else None
            row["activity_name"] = activity.name if activity else None
            row["pace_at_hr"] = None
            rows.append(row)
        best = min(rows, key=lambda r: r["duration_s"]) if rows else None
        return {
            "segment": seg.as_dict(),
            "count": len(rows),
            "activities": len({r["activity_id"] for r in rows}),
            "best": best,
            "efforts": rows,
        }

    def activity_efforts(self, activity_id: int) -> list[dict[str, Any]]:
        from thresy.analysis.segments import with_derived

        out = []
        for seg in self.segments():
            for e in self.data.efforts.get(seg.key, []):
                if e["activity_id"] == activity_id:
                    item = {k: v for k, v in e.items() if k != "activity_id"}
                    out.append(with_derived({"segment_key": seg.key, "segment": seg.name, **item}))
        return sorted(out, key=lambda e: e["start_utc"])

    def planned_for(self, activity_id: int) -> list[dict[str, Any]]:
        return [dict(w) for w in self.data.workouts if w["activity_id"] == activity_id]

    def coach_message(self) -> dict[str, Any] | None:
        if not self.data.messages:
            return None
        return dict(max(self.data.messages, key=lambda m: m["ts"]))

    def state(self) -> dict[str, Any]:
        return {k: dict(v) for k, v in self.data.state.items()}

    def counts(self) -> dict[str, Any]:
        runs = [a for a in self.data.activities if a["is_run"]]
        return {
            "runs": len(runs),
            "with_records": sum(1 for a in runs if a["has_records"]),
            "first_run": min((a["local_date"] for a in runs), default=None),
            "last_run": max((a["local_date"] for a in runs), default=None),
            "nights": sum(1 for w in self.data.wellness.values() if w["sleep_s"]),
        }


@lru_cache(maxsize=16)
def _simulated(
    activity_id: int, scenario: str, lang: str
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    return simulate(build(scenario, lang).specs[activity_id])
