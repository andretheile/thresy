"""One activity in detail: splits, halves, heart-rate drift, climbs and stops.

All functions take a record frame from :func:`thresy.analysis.data.load_records`. Times are
timer times (watch pauses excluded) unless a field says ``elapsed``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

from thresy.analysis.data import (
    MOVING_SPEED_MPS,
    Activity,
    ascent_profile,
    first_reach,
    max_or_none,
    mean_or_none,
    pace_s_per_km,
    smooth_altitude,
)
from thresy.analysis.fmt import rounded

DRIFT_SKIP_S = 600.0
"""Heart-rate drift ignores the first 10 minutes of timer time (warm-up, heart rate rising)."""
DRIFT_MIN_S = 1200.0
"""Drift needs at least 20 minutes of moving time after the skipped warm-up."""
CLIMB_REVERSAL_M = 10.0
"""A climb ends once the smoothed altitude has dropped this far below its highest point."""
CLIMB_MIN_GAIN_M = 25.0
CLIMB_MIN_GRADE_PCT = 2.0
CLIMB_TRIM_M = 0.5
"""A climb starts at the last sample within this many metres of its lowest point (flat
stretches before the hill are not part of it)."""
STOP_MIN_S = 20.0
"""Stops shorter than this are counted in the totals but not listed."""


def _index_at_distance(dist: np.ndarray, x: float) -> int:
    return min(int(np.searchsorted(dist, x, side="left")), len(dist) - 1)


def _moving(df: pd.DataFrame) -> pd.Series:
    speed = df["speed"] if "speed" in df else pd.Series(np.nan, index=df.index)
    return (~df["paused"]) & (speed.fillna(0.0) >= MOVING_SPEED_MPS)


def _cadence(df: pd.DataFrame) -> float | None:
    if "cad" not in df:
        return None
    cad = df.loc[_moving(df), "cad"]
    return mean_or_none(cad[cad > 0], 0)


@dataclass
class Span:
    """Aggregates over a stretch of an activity."""

    start_m: float
    end_m: float
    dist_m: float
    time_s: float
    elapsed_s: float
    pace_s_per_km: float | None
    avg_hr: float | None
    max_hr: float | None
    ascent_m: float
    descent_m: float
    cadence_spm: float | None
    power_w: float | None
    stopped_s: float

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, float):
                out[key] = round(value, 1)
        return out


class Profile:
    """Precomputed arrays of one record frame, to aggregate many stretches quickly."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df
        self.dist = df["dist"].to_numpy(dtype="float64")
        self.timer = df["timer"].to_numpy(dtype="float64")
        self.t = df["t"].to_numpy(dtype="float64")
        self.alt = smooth_altitude(df)
        self.up, self.down = ascent_profile(self.alt)
        stop_s = df["stop_s"].to_numpy(dtype="float64")
        self.stopped = np.r_[0.0, np.cumsum(stop_s)]  # stopped before sample k: [:k]

    def span(self, i: int, j: int) -> Span:
        """Aggregates from sample ``i`` to sample ``j`` (inclusive)."""
        df = self.df
        rows = df.iloc[i : j + 1]
        dist = float(self.dist[j] - self.dist[i])
        time_s = float(self.timer[j] - self.timer[i])
        moving = _moving(rows)
        power = rows.loc[moving, "power"] if "power" in rows else pd.Series(dtype=float)
        return Span(
            start_m=float(self.dist[i]),
            end_m=float(self.dist[j]),
            dist_m=dist,
            time_s=time_s,
            elapsed_s=float(self.t[j] - self.t[i]),
            pace_s_per_km=pace_s_per_km(time_s, dist),
            avg_hr=mean_or_none(rows["hr"], 0) if "hr" in rows else None,
            max_hr=max_or_none(rows["hr"]) if "hr" in rows else None,
            ascent_m=float(self.up[j] - self.up[i]),
            descent_m=float(self.down[j] - self.down[i]),
            cadence_spm=_cadence(rows),
            power_w=mean_or_none(power, 0),
            stopped_s=float(self.stopped[j] - self.stopped[i]),
        )

    def index_at(self, x: float) -> int:
        return _index_at_distance(self.dist, x)


def splits(df: pd.DataFrame, split_m: float = 1000.0, profile: Profile | None = None) -> list[dict]:
    """Per-distance splits (default 1 km); the last one may be shorter."""
    if df.empty or df["dist"].iloc[-1] <= 0:
        return []
    p = profile or Profile(df)
    total = float(p.dist[-1])
    bounds = [*np.arange(0.0, total, split_m).tolist(), total]
    out = []
    for n, (a, b) in enumerate(pairwise(bounds), start=1):
        if b - a < 1.0:
            continue
        i = 0 if a == 0 else p.index_at(a)
        j = p.index_at(b) if b < total else len(df) - 1
        span = p.span(i, j)
        # time at the exact boundary distances, so splits add up to the total timer time
        t_a = 0.0 if a == 0 else first_reach(p.dist, p.timer, a)
        t_b = first_reach(p.dist, p.timer, b) if b < total else float(p.timer[-1])
        span.time_s = t_b - t_a
        span.dist_m = b - a
        span.start_m, span.end_m = a, b
        span.pace_s_per_km = pace_s_per_km(span.time_s, span.dist_m)
        out.append({"n": n, **span.as_dict()})
    return out


def halves(df: pd.DataFrame, profile: Profile | None = None) -> dict[str, Any] | None:
    """First and second half by distance, and the difference (second minus first)."""
    if df.empty or df["dist"].iloc[-1] <= 0:
        return None
    p = profile or Profile(df)
    mid = p.index_at(float(p.dist[-1]) / 2)
    first, second = p.span(0, mid), p.span(mid, len(df) - 1)

    def diff(key: str) -> float | None:
        a, b = getattr(first, key), getattr(second, key)
        return None if a is None or b is None else round(b - a, 1)

    return {
        "first": first.as_dict(),
        "second": second.as_dict(),
        "pace_diff_s_per_km": diff("pace_s_per_km"),
        "hr_diff": diff("avg_hr"),
        "negative_split": bool(
            first.pace_s_per_km
            and second.pace_s_per_km
            and second.pace_s_per_km < first.pace_s_per_km
        ),
    }


def hr_drift(df: pd.DataFrame, skip_s: float = DRIFT_SKIP_S) -> dict[str, Any]:
    """Heart-rate drift and aerobic decoupling.

    The moving samples after the first ``skip_s`` seconds of timer time are split into two
    halves of equal timer time. For each half: mean heart rate, speed (distance / timer time)
    and the efficiency factor EF = speed in m/min / heart rate (metres per beat).
    ``drift_bpm`` = HR(second) - HR(first); ``decoupling_pct`` = (EF1 - EF2) / EF1 * 100
    (Pa:HR; under about 5 % means the pace was held without the heart rate creeping up).
    Hills distort both numbers, so the ascent of each half is reported as well.
    """
    out: dict[str, Any] = {"skip_s": skip_s}
    if df.empty or "hr" not in df or df["hr"].notna().sum() < 60:
        out["note"] = "no heart rate"
        return out
    rows = df[(df["timer"] >= skip_s) & _moving(df) & df["hr"].notna()]
    if rows.empty or rows["timer"].iloc[-1] - rows["timer"].iloc[0] < DRIFT_MIN_S:
        out["note"] = (
            f"too short (needs {DRIFT_MIN_S / 60:.0f} min after the first {skip_s / 60:.0f})"
        )
        return out
    t0, t1 = float(rows["timer"].iloc[0]), float(rows["timer"].iloc[-1])
    mid = (t0 + t1) / 2
    alt = smooth_altitude(df)
    up, _ = ascent_profile(alt)
    halves_out = []
    for lo, hi in ((t0, mid), (mid, t1)):
        part = rows[(rows["timer"] >= lo) & (rows["timer"] <= hi)]
        i, j = int(part.index[0]), int(part.index[-1])
        time_s = float(df["timer"].iloc[j] - df["timer"].iloc[i])
        dist = float(df["dist"].iloc[j] - df["dist"].iloc[i])
        hr = float(part["hr"].mean())
        speed = dist / time_s if time_s > 0 else float("nan")
        halves_out.append(
            {
                "hr": round(hr, 1),
                "pace_s_per_km": rounded(pace_s_per_km(time_s, dist)),
                "ef_m_per_beat": round(speed * 60 / hr, 3) if hr > 0 else None,
                "ascent_m": round(float(up[j] - up[i]), 1),
            }
        )
    first, second = halves_out
    out.update(
        first=first,
        second=second,
        window_s=round(t1 - t0),
        drift_bpm=round(second["hr"] - first["hr"], 1),
        decoupling_pct=(
            round(
                (first["ef_m_per_beat"] - second["ef_m_per_beat"]) / first["ef_m_per_beat"] * 100, 1
            )
            if first["ef_m_per_beat"] and second["ef_m_per_beat"]
            else None
        ),
    )
    return out


def turning_points(alt: np.ndarray, reversal_m: float) -> list[int]:
    """Indices where a smoothed altitude series turns (zigzag with ``reversal_m`` hysteresis),
    including the first and last index."""
    n = len(alt)
    if n == 0:
        return []
    points = [0]
    rising = True
    ext = 0
    for k in range(1, n):
        if rising:
            if alt[k] > alt[ext]:
                ext = k
            elif alt[ext] - alt[k] > reversal_m:
                points.append(ext)
                rising, ext = False, k
        else:
            if alt[k] < alt[ext]:
                ext = k
            elif alt[k] - alt[ext] > reversal_m:
                points.append(ext)
                rising, ext = True, k
    if points[-1] != n - 1:
        points.append(n - 1)
    return sorted(set(points))


def trim_flat(alt: np.ndarray, s: int, e: int, tol_m: float = CLIMB_TRIM_M) -> tuple[int, int]:
    """Move the start of a climb (descent) from turning point ``s`` to the last sample within
    ``tol_m`` of its lowest (highest) altitude before ``e``.

    The zigzag in :func:`turning_points` ends a climb at the first sample of its top, but starts
    it at the first sample of the low point before, so a flat stretch (or a small dip) before
    the hill would otherwise count as part of it.
    """
    seg = alt[s : e + 1]
    if alt[e] >= alt[s]:
        near = np.flatnonzero(seg <= np.nanmin(seg) + tol_m)
    else:
        near = np.flatnonzero(seg >= np.nanmax(seg) - tol_m)
    new_s = s + int(near.max()) if near.size else s
    return (new_s, e) if new_s < e else (s, e)


def climbs(
    df: pd.DataFrame,
    *,
    reversal_m: float = CLIMB_REVERSAL_M,
    min_gain_m: float = CLIMB_MIN_GAIN_M,
    min_grade_pct: float = CLIMB_MIN_GRADE_PCT,
    descents: bool = False,
    profile: Profile | None = None,
) -> list[dict[str, Any]]:
    """Climbs (and with ``descents`` also descents) found in the altitude profile.

    The altitude is smoothed (centred mean over 15 samples) and cut at its turning points: a
    climb runs from a low point to the next high point and ends once the altitude has fallen
    ``reversal_m`` below that high point; flat stretches at either end are trimmed
    (:func:`trim_flat`). Kept are climbs with at least ``min_gain_m`` net gain
    and ``min_grade_pct`` average grade. ``vam`` is the climbing rate in metres per hour.
    """
    if df.empty:
        return []
    p = profile or Profile(df)
    if np.isnan(p.alt).all():
        return []
    points = turning_points(p.alt, reversal_m)
    out = []
    for s0, e0 in pairwise(points):
        gain = float(p.alt[e0] - p.alt[s0])
        direction = "up" if gain > 0 else "down"
        if direction == "down" and not descents:
            continue
        if abs(gain) < min_gain_m:
            continue
        s, e = trim_flat(p.alt, s0, e0)
        gain = float(p.alt[e] - p.alt[s])
        span = p.span(s, e)
        if span.dist_m <= 0:
            continue
        grade = gain / span.dist_m * 100
        if abs(grade) < min_grade_pct:
            continue
        out.append(
            {
                "n": len(out) + 1,
                "direction": direction,
                "start_km": round(span.start_m / 1000, 2),
                "end_km": round(span.end_m / 1000, 2),
                "dist_m": round(span.dist_m),
                "gain_m": round(gain, 1),
                "grade_pct": round(grade, 1),
                "alt_from_m": round(float(p.alt[s]), 1),
                "alt_to_m": round(float(p.alt[e]), 1),
                "time_s": round(span.time_s, 1),
                "pace_s_per_km": rounded(span.pace_s_per_km),
                "vam_m_per_h": round(gain / span.time_s * 3600) if span.time_s > 0 else None,
                "avg_hr": span.avg_hr,
                "max_hr": span.max_hr,
                "power_w": span.power_w,
                "t_start": float(p.t[s]),
                "t_end": float(p.t[e]),
            }
        )
    return out


def stops(df: pd.DataFrame, activity: Activity | None = None, min_s: float = STOP_MIN_S) -> dict:
    """Time spent stopped: watch pauses (gaps between records) and standing still while the
    timer ran (speed below 0.5 m/s). Stops of at least ``min_s`` seconds are listed."""
    if df.empty:
        return {"paused_s": 0.0, "standing_s": 0.0, "stops": []}
    paused_s = float(df.loc[df["paused"], "dt"].sum())
    standing_s = float(df["still_s"].sum())
    listed = []
    # paused gaps
    for idx in df.index[df["paused"]]:
        listed.append(
            (float(df.at[idx, "t"] - df.at[idx, "dt"]), float(df.at[idx, "dt"]), idx, "paused")
        )
    # standing stretches (consecutive still samples)
    still = df["still"].to_numpy()
    if still.any():
        groups = np.cumsum(np.r_[True, still[1:] != still[:-1]])
        for g in np.unique(groups[still]):
            rows = df[groups == g]
            secs = float(rows["still_s"].sum())
            listed.append((float(rows["t"].iloc[0]), secs, rows.index[0], "standing"))
    listed = [s for s in listed if s[1] >= min_s]
    listed.sort()
    out = []
    for t_start, secs, idx, kind in listed:
        item = {
            "kind": kind,
            "at_km": round(float(df.at[idx, "dist"]) / 1000, 2),
            "duration_s": round(secs),
            "t_start": round(t_start),
        }
        if activity is not None and "ts" in df:
            start_ts = df.at[idx, "ts"] - pd.Timedelta(seconds=float(df.at[idx, "dt"]))
            item["start_local"] = activity.local(start_ts if kind == "paused" else df.at[idx, "ts"])
        out.append(item)
    return {"paused_s": round(paused_s), "standing_s": round(standing_s), "stops": out}


def summarize(
    activity: Activity,
    df: pd.DataFrame,
    *,
    split_m: float = 1000.0,
    climb_min_gain_m: float = CLIMB_MIN_GAIN_M,
    descents: bool = False,
) -> dict[str, Any]:
    """Everything ``coach analysis activity`` reports, as one dict."""
    p = Profile(df)
    total = p.span(0, len(df) - 1) if len(df) else None
    resp = mean_or_none(df.loc[_moving(df), "resp"], 1) if "resp" in df else None
    totals = None
    if total is not None:
        totals = {
            **total.as_dict(),
            "moving_s": round(total.time_s - float(df["still_s"].sum())),
            "resp_rate": resp,
            "garmin_elev_gain_m": activity.elev_gain_m,
            "samples": len(df),
        }
    return {
        "activity": activity.as_dict(),
        "totals": totals,
        "splits": splits(df, split_m, p),
        "halves": halves(df, p),
        "drift": hr_drift(df),
        "climbs": climbs(df, min_gain_m=climb_min_gain_m, descents=descents, profile=p),
        "stops": stops(df, activity),
    }
