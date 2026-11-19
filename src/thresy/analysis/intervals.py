"""Interval sessions: work and recovery segments and the metrics of each repetition.

Detection (``source="auto"`` tries these in order):

1. **Workout laps**: laps carry a workout step (``wkt_step``) and their ``intensity`` names both
   work (``active``/``interval``) and rest (``rest``/``recovery``) steps. Work laps are the reps.
2. **Manual laps**: at least :data:`MIN_MANUAL_LAPS` laps were ended by a lap-button press.
   The laps ended by a press, plus the lap after each press, are split into fast and slow by
   their average power (when every one of them has power) or speed, using the split that
   maximises the between-group variance (two-means). Accepted when there are at least two work
   laps, the work group is at least :data:`MIN_RATIO` times the slow group, and the stretch
   from the first to the last work lap consists only of such laps (so a few stray presses in an
   auto-lap run do not count). Slow laps before the first and after the last rep are warm-up
   and cool-down.
3. **Records**: the per-second speed (or power with ``signal="power"``, which suits hill
   repeats because power rises uphill where speed falls, but also flags hilly warm-ups) is
   smoothed with a centred 5 s median and a 5 s mean and split into fast and slow by two-means
   over the moving samples. Accepted when the groups are clearly separated (Ashman's
   D >= :data:`MIN_SEPARATION` and ratio >= :data:`MIN_RATIO`). Work = stretches above the
   threshold; dips shorter than :data:`MERGE_GAP_S` are bridged and stretches shorter than
   :data:`MIN_WORK_S` dropped. Edges found this way trail the athlete's lap presses by about
   2-7 s (the time it takes to speed up or slow down), so laps are preferred when they exist.

Per repetition (from the records inside the rep's time window): timer time, distance, pace,
average and maximum heart rate, heart rate over the last 30 s, heart rate at the end (last 5 s),
heart-rate drop in the first 60 s of recovery (end value minus the mean of 57-63 s after the
rep; measured even when the recovery is shorter, so check ``recovery_s``), cadence (moving
samples), stride length (speed / cadence), breathing rate (mean and max) and power.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from thresy.analysis.data import (
    MOVING_SPEED_MPS,
    Activity,
    Conn,
    find_activities,
    load_laps,
    load_records,
    max_or_none,
    mean_or_none,
    min_or_none,
    pace_s_per_km,
    window,
)
from thresy.analysis.fmt import rounded

MIN_WORK_S = 15.0
MERGE_GAP_S = 10.0
MIN_SEPARATION = 2.0
MIN_RATIO = 1.15
MIN_MANUAL_LAPS = 3
POWER_COVERAGE = 0.8
HR_DROP_AFTER_S = 60.0
PREFILTER_SPEED_CV = 0.10
"""Session comparison only runs detection on runs with laps from a workout or lap presses, or
whose speed varies (coefficient of variation of moving speed at least this)."""

WORK_INTENSITIES = frozenset({"active", "interval"})
REST_INTENSITIES = frozenset({"rest", "recovery"})


@dataclass
class Detection:
    source: str  # "workout" | "laps" | "records" | "none"
    signal: str | None  # "intensity" | "power" | "speed"
    work: list[tuple[float, float]] = field(default_factory=list)
    threshold: float | None = None
    note: str | None = None


def two_means(values: np.ndarray) -> tuple[float, float, float, float, float] | None:
    """Best split of 1-D values into a low and a high group (maximal between-group variance).

    Returns ``(threshold, low_mean, high_mean, low_std, high_std)`` or ``None`` for fewer than
    two distinct values.
    """
    v = np.sort(np.asarray(values, dtype="float64"))
    v = v[~np.isnan(v)]
    n = v.size
    if n < 2 or v[0] == v[-1]:
        return None
    cs = np.cumsum(v)
    k = np.arange(1, n)
    m1 = cs[:-1] / k
    m2 = (cs[-1] - cs[:-1]) / (n - k)
    between = k * (n - k) * (m1 - m2) ** 2
    between[v[k] == v[k - 1]] = -1  # never split between equal values
    best = int(np.argmax(between)) + 1
    lo, hi = v[:best], v[best:]
    return (
        float((v[best - 1] + v[best]) / 2),
        float(lo.mean()),
        float(hi.mean()),
        float(lo.std()),
        float(hi.std()),
    )


# Detection from laps --------------------------------------------------------------------------


def _lap_signal(laps: pd.DataFrame) -> tuple[pd.Series, str]:
    power = laps.get("avg_power_w")
    if power is not None and power.notna().all() and (power > 0).all():
        return power.astype(float), "power"
    duration = laps["duration_s"].where(laps["duration_s"] > 0)
    return (laps["dist_m"] / duration).astype(float), "speed"


def _trim(work: list[bool]) -> tuple[int, int] | None:
    idx = [i for i, w in enumerate(work) if w]
    return (idx[0], idx[-1]) if idx else None


def detect_from_laps(laps: pd.DataFrame) -> Detection | None:
    """Work segments from the laps, or ``None`` when the laps do not describe the session."""
    if laps.empty or len(laps) < 3 or "t_start" not in laps:
        return None
    laps = laps.reset_index(drop=True)
    intensity = laps["intensity"].fillna("").astype(str)
    if laps["wkt_step"].notna().sum() >= 2:
        work = intensity.isin(WORK_INTENSITIES)
        rest = intensity.isin(REST_INTENSITIES)
        if work.sum() >= 2 and rest.sum() >= 1:
            segs = [(float(r.t_start), float(r.t_end)) for r in laps[work].itertuples()]
            return Detection("workout", "intensity", segs)

    manual = laps["trigger"].fillna("") == "manual"
    if manual.sum() < MIN_MANUAL_LAPS:
        return None
    candidate = manual | manual.shift(1, fill_value=False)
    cand = laps[candidate & (laps["duration_s"] > 0)]
    if len(cand) < 3:
        return None
    signal, name = _lap_signal(cand)
    split = two_means(signal.to_numpy())
    if split is None:
        return None
    threshold, lo, hi, _, _ = split
    if lo <= 0 or hi / lo < MIN_RATIO:
        return None
    is_work = signal >= threshold
    if is_work.sum() < 2:
        return None
    first_work = int(is_work[is_work].index[0])
    last_work = int(is_work[is_work].index[-1])
    core = laps.loc[first_work:last_work]
    if not candidate.loc[first_work:last_work].all() or (core["duration_s"] <= 0).any():
        return None  # stray lap presses in an otherwise auto-lap run
    segs = [
        (float(laps.at[i, "t_start"]), float(laps.at[i, "t_end"])) for i in is_work[is_work].index
    ]
    return Detection("laps", name, segs, threshold)


# Detection from records -----------------------------------------------------------------------


def _moving_mask(df: pd.DataFrame) -> pd.Series:
    return (~df["paused"]) & (df["speed"].fillna(0.0) >= MOVING_SPEED_MPS)


def _smoothed(df: pd.DataFrame, col: str) -> pd.Series:
    s = df[col].astype(float).where(~df["paused"])
    s.index = pd.to_timedelta(df["t"], unit="s")
    s = s.rolling("5s", center=True, min_periods=1).median()
    s = s.rolling("5s", center=True, min_periods=1).mean()
    return pd.Series(s.to_numpy(), index=df.index)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges ``[i, j]`` (inclusive) of consecutive True values."""
    if mask.size == 0:
        return []
    edges = np.diff(np.r_[0, mask.astype(np.int8), 0])
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1) - 1
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def _has_power(df: pd.DataFrame) -> bool:
    if "power" not in df:
        return False
    moving = _moving_mask(df)
    return bool(moving.any()) and (df.loc[moving, "power"].fillna(0) > 0).mean() >= POWER_COVERAGE


def detect_from_records(
    df: pd.DataFrame, *, min_work_s: float = MIN_WORK_S, signal: str | None = None
) -> Detection:
    """Work segments from the per-second speed (default) or power (see the module docstring)."""
    if df.empty or "speed" not in df:
        return Detection("none", None, note="no speed data")
    moving = _moving_mask(df)
    signal = signal or "speed"
    if signal not in ("speed", "power"):
        raise ValueError(f"unknown signal {signal!r}")
    if signal == "power" and not _has_power(df):
        return Detection("none", signal, note="no power data")
    x = _smoothed(df, signal)
    values = x[moving].dropna().to_numpy()
    if signal == "power":
        values = values[values > 0]
    split = two_means(values) if values.size >= 60 else None
    if split is None:
        return Detection("none", signal, note="not enough moving data")
    threshold, lo, hi, sd_lo, sd_hi = split
    separation = math.sqrt(2) * (hi - lo) / math.sqrt(sd_lo**2 + sd_hi**2 + 1e-12)
    if lo <= 0 or hi / lo < MIN_RATIO or separation < MIN_SEPARATION:
        ratio = hi / max(lo, 1e-9)
        note = f"no clear fast/slow structure (ratio {ratio:.2f}, D {separation:.1f})"
        return Detection("none", signal, threshold=threshold, note=note)
    t = df["t"].to_numpy()
    mask = (x >= threshold).to_numpy() & (~df["paused"]).to_numpy()
    # bridge short dips inside a rep
    runs = _runs(mask)
    merged: list[list[int]] = []
    for i, j in runs:
        if merged and t[i] - t[merged[-1][1]] <= MERGE_GAP_S:
            merged[-1][1] = j
        else:
            merged.append([i, j])
    timer = df["timer"].to_numpy()
    segs = []
    for i, j in merged:
        end_t = t[j + 1] if j + 1 < len(t) else t[j] + 1.0
        if timer[j] - timer[i] + 1.0 < min_work_s:
            continue
        segs.append((float(t[i]), float(end_t)))
    if len(segs) < 2:
        return Detection("none", signal, threshold=threshold, note="fewer than two work stretches")
    return Detection("records", signal, segs, threshold)


def detect(
    df: pd.DataFrame,
    laps: pd.DataFrame,
    source: str = "auto",
    *,
    min_work_s: float = MIN_WORK_S,
    signal: str | None = None,
) -> Detection:
    """Work segments by ``source``: ``auto`` (laps, then records), ``laps`` or ``records``.

    ``signal`` (``speed`` or ``power``) applies to detection from records; speed is the default
    because power also rises on uphill jogging. Use power for hill repeats.
    """
    if source not in ("auto", "laps", "records"):
        raise ValueError(f"unknown source {source!r}")
    if source in ("auto", "laps"):
        found = detect_from_laps(laps)
        if found is not None or source == "laps":
            return found or Detection("none", None, note="the laps do not describe intervals")
    return detect_from_records(df, min_work_s=min_work_s, signal=signal)


# Metrics --------------------------------------------------------------------------------------


def _at(df: pd.DataFrame, col: str, t: float) -> float:
    valid = df[["t", col]].dropna()
    if valid.empty:
        return float("nan")
    return float(np.interp(t, valid["t"].to_numpy(), valid[col].to_numpy()))


def rep_metrics(
    df: pd.DataFrame, start_t: float, end_t: float, next_start_t: float | None
) -> dict[str, Any]:
    w = window(df, start_t, end_t)
    time_s = _at(df, "timer", end_t) - _at(df, "timer", start_t)
    dist = _at(df, "dist", end_t) - _at(df, "dist", start_t)
    hr = w["hr"] if "hr" in w else pd.Series(dtype=float)
    last30 = window(df, end_t - 30, end_t)["hr"] if "hr" in df else hr
    end5 = window(df, end_t - 5, end_t)["hr"] if "hr" in df else hr
    hr_end = mean_or_none(end5, 1)
    after = window(df, end_t + HR_DROP_AFTER_S - 3, end_t + HR_DROP_AFTER_S + 3)
    hr_after = mean_or_none(after["hr"], 1) if "hr" in after else None
    moving = _moving_mask(w)
    cad = w.loc[moving, "cad"] if "cad" in w else pd.Series(dtype=float)
    cadence = mean_or_none(cad[cad > 0], 1)
    speed = dist / time_s if time_s > 0 else None
    stride = speed / (cadence / 60) if speed and cadence else None
    return {
        "t_start": round(start_t, 1),
        "t_end": round(end_t, 1),
        "time_s": round(time_s, 1),
        "dist_m": round(dist, 1),
        "pace_s_per_km": rounded(pace_s_per_km(time_s, dist)),
        "avg_hr": mean_or_none(hr, 1),
        "max_hr": max_or_none(hr),
        "hr_last30": mean_or_none(last30, 1),
        "hr_end": hr_end,
        "hr_drop_60": round(hr_end - hr_after, 1)
        if hr_end is not None and hr_after is not None
        else None,
        "recovery_s": round(next_start_t - end_t, 1) if next_start_t is not None else None,
        "cadence_spm": cadence,
        "stride_m": round(stride, 2) if stride else None,
        "resp_avg": mean_or_none(w["resp"], 1) if "resp" in w else None,
        "resp_max": max_or_none(w["resp"]) if "resp" in w else None,
        "power_w": mean_or_none(w.loc[moving, "power"], 0) if "power" in w else None,
    }


def recovery_metrics(df: pd.DataFrame, start_t: float, end_t: float) -> dict[str, Any]:
    w = window(df, start_t, end_t)
    time_s = _at(df, "timer", end_t) - _at(df, "timer", start_t)
    dist = _at(df, "dist", end_t) - _at(df, "dist", start_t)
    return {
        "t_start": round(start_t, 1),
        "t_end": round(end_t, 1),
        "time_s": round(time_s, 1),
        "dist_m": round(dist, 1),
        "pace_s_per_km": rounded(pace_s_per_km(time_s, dist)),
        "avg_hr": mean_or_none(w["hr"], 1) if "hr" in w else None,
        "min_hr": min_or_none(w["hr"]) if "hr" in w else None,
    }


def session_summary(reps: list[dict[str, Any]]) -> dict[str, Any]:
    """Averages over reps: pace from total time and distance, the rest as mean of the reps."""
    if not reps:
        return {"n": 0}
    time_s = sum(r["time_s"] for r in reps)
    dist = sum(r["dist_m"] for r in reps)

    def avg(key: str, digits: int = 1) -> float | None:
        values = [r[key] for r in reps if r.get(key) is not None]
        return round(sum(values) / len(values), digits) if values else None

    recoveries = [r["recovery_s"] for r in reps if r.get("recovery_s") is not None]
    first, last = reps[0].get("avg_hr"), reps[-1].get("avg_hr")
    return {
        "n": len(reps),
        "time_s": round(time_s, 1),
        "dist_m": round(dist, 1),
        "pace_s_per_km": rounded(pace_s_per_km(time_s, dist)),
        "avg_hr": avg("avg_hr"),
        "hr_last30": avg("hr_last30"),
        "max_hr": max((r["max_hr"] for r in reps if r.get("max_hr") is not None), default=None),
        "hr_drop_60": avg("hr_drop_60"),
        "recovery_s": float(np.median(recoveries)) if recoveries else None,
        "cadence_spm": avg("cadence_spm"),
        "stride_m": avg("stride_m", 2),
        "resp_avg": avg("resp_avg"),
        "power_w": avg("power_w", 0),
        "hr_first_to_last": round(last - first, 1)
        if first is not None and last is not None
        else None,
    }


def analyze(
    activity: Activity,
    df: pd.DataFrame,
    laps: pd.DataFrame,
    *,
    source: str = "auto",
    min_work_s: float = MIN_WORK_S,
    signal: str | None = None,
) -> dict[str, Any]:
    """Detect the work segments of one activity and compute the per-rep metrics."""
    det = detect(df, laps, source, min_work_s=min_work_s, signal=signal)
    reps, recoveries = [], []
    starts = [s for s, _ in det.work]
    for n, (s, e) in enumerate(det.work):
        nxt = starts[n + 1] if n + 1 < len(starts) else None
        rep = {"n": n + 1, **rep_metrics(df, s, e, nxt)}
        if "ts" in df and len(df):
            ts = df["ts"].iloc[0] + pd.Timedelta(seconds=s)
            rep["start_local"] = activity.local(ts)
        reps.append(rep)
        if nxt is not None:
            recoveries.append({"after_rep": n + 1, **recovery_metrics(df, e, nxt)})
    return {
        "activity": activity.as_dict(),
        "source": det.source,
        "signal": det.signal,
        "threshold": rounded(det.threshold, 2),
        "note": det.note,
        "reps": reps,
        "recoveries": recoveries,
        "summary": session_summary(reps),
    }


# Comparing sessions ---------------------------------------------------------------------------


def matches(
    rep: dict[str, Any], rep_m: float | None, rep_s: float | None, tolerance: float
) -> bool:
    if rep_m is not None and abs(rep["dist_m"] - rep_m) > tolerance * rep_m:
        return False
    return not (rep_s is not None and abs(rep["time_s"] - rep_s) > tolerance * rep_s)


def _candidates(conn: Conn, ids: list[int]) -> list[int]:
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT a.id
        FROM unnest(%s::bigint[]) AS a(id)
        WHERE (SELECT count(*) FROM activity_laps l
               WHERE l.activity_id = a.id AND (l.trigger = 'manual' OR l.wkt_step IS NOT NULL)) >= 3
           OR (SELECT stddev_samp(r.speed_mps) / nullif(avg(r.speed_mps), 0)
               FROM activity_records r
               WHERE r.activity_id = a.id AND r.speed_mps >= %s) >= %s
        """,
        (ids, MOVING_SPEED_MPS, PREFILTER_SPEED_CV),
    ).fetchall()
    keep = {r["id"] if isinstance(r, dict) else r[0] for r in rows}
    return [i for i in ids if i in keep]


def compare_sessions(
    conn: Conn,
    *,
    rep_m: float | None = None,
    rep_s: float | None = None,
    tolerance: float = 0.15,
    since: date | None = None,
    until: date | None = None,
    min_reps: int = 3,
    first_n: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Interval sessions whose reps are about ``rep_m`` metres and/or ``rep_s`` seconds long.

    Every run with per-second data in the date range that passes the cheap prefilter (see
    :data:`PREFILTER_SPEED_CV`) is analysed; a session qualifies with at least ``min_reps``
    matching reps (``|value - target| <= tolerance * target``). Other reps of the session (a
    final 200 m, say) are ignored. ``first_n`` averages only the first N matching reps so that
    sessions with different numbers of reps compare fairly.
    """
    if rep_m is None and rep_s is None:
        raise ValueError("give a rep distance (rep_m) or duration (rep_s)")
    activities = {
        a.id: a
        for a in find_activities(
            conn, since=since, until=until, with_records=True, newest_first=False
        )
    }
    ids = _candidates(conn, list(activities))
    sessions = []
    for n, activity_id in enumerate(ids, start=1):
        activity = activities[activity_id]
        df = load_records(conn, activity_id, ["dist", "speed", "hr", "cad", "power", "resp"])
        laps = load_laps(conn, activity_id, df["ts"].iloc[0] if len(df) else None)
        result = analyze(activity, df, laps)
        reps = [r for r in result["reps"] if matches(r, rep_m, rep_s, tolerance)]
        if progress and n % 25 == 0:
            progress(f"analysed {n}/{len(ids)} candidate runs")
        if len(reps) < min_reps:
            continue
        used = reps[:first_n] if first_n else reps
        sessions.append(
            {
                "activity_id": activity_id,
                "date": activity.local_date,
                "name": activity.name,
                "type": activity.type,
                "source": result["source"],
                "n_matching": len(reps),
                "n_reps": len(result["reps"]),
                **{k: v for k, v in session_summary(used).items() if k != "n"},
                "n_used": len(used),
                "reps": used,
            }
        )
    return {
        "rep_m": rep_m,
        "rep_s": rep_s,
        "tolerance": tolerance,
        "first_n": first_n,
        "scanned": len(activities),
        "analysed": len(ids),
        "sessions": sessions,
    }
