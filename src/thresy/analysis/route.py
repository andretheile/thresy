"""Two activities on the same course, aligned by position; and finding such activities.

Alignment (:func:`compare`): the first activity (the reference, typically the earlier one) is
the course. Its GPS track is projected to metres and its own path length along the track is the
*course distance*. Every sample of the second activity is projected onto the nearest point of
the reference track within :data:`MAX_OFFSET_M`, searching only a window around the progress so
far (:data:`BACK_M` behind, :data:`AHEAD_M` ahead), so that out-and-back courses and loops are
followed in order. Progress never decreases; samples farther than :data:`MAX_OFFSET_M` from the
course are off-route.

For both activities, the elapsed (wall-clock) time at which each course distance is first
reached gives ``time(x)``. The comparison covers the common part of the course, from the later
of the two first on-route points to the earlier of the two last ones, and every time is measured
from the start of that common part. ``delta(x) = time_B(x) - time_A(x)``: positive means the
second activity is behind. Stops count, since they cost time; the standing time inside the
common part is reported separately.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

from thresy.analysis.activity import CLIMB_MIN_GAIN_M, turning_points
from thresy.analysis.data import (
    Activity,
    Conn,
    ascent_profile,
    find_activities,
    first_reach,
    load_records,
    mean_or_none,
)
from thresy.analysis.fmt import rounded

M_PER_DEG = 111320.0
MAX_OFFSET_M = 40.0
BACK_M = 100.0
AHEAD_M = 400.0
RESAMPLE_M = 5.0
COVER_CELL_M = 40.0


def _project(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    x = (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0))
    y = (lat - lat0) * M_PER_DEG
    return np.column_stack([x, y])


def reference_track(
    df: pd.DataFrame, origin: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The reference polyline resampled to points at least :data:`RESAMPLE_M` apart.

    Returns ``(xy, cum, idx)``: points in metres, path length at each point, and the index of
    the record each point came from.
    """
    pos = df[df["lat"].notna() & df["lon"].notna()]
    xy = _project(pos["lat"].to_numpy(), pos["lon"].to_numpy(), *origin)
    keep = [0]
    for i in range(1, len(xy)):
        if np.hypot(*(xy[i] - xy[keep[-1]])) >= RESAMPLE_M:
            keep.append(i)
    if keep[-1] != len(xy) - 1 and len(xy) > 1:
        keep.append(len(xy) - 1)
    pts = xy[keep]
    seg = np.hypot(*np.diff(pts, axis=0).T) if len(pts) > 1 else np.zeros(0)
    cum = np.r_[0.0, np.cumsum(seg)]
    return pts, cum, pos.index.to_numpy()[keep]


def project_progress(
    ref_xy: np.ndarray, ref_cum: np.ndarray, xy: np.ndarray, *, max_offset_m: float = MAX_OFFSET_M
) -> tuple[np.ndarray, np.ndarray]:
    """Course distance and offset for each point of ``xy`` (NaN progress when off-route)."""
    a = ref_xy[:-1]
    d = np.diff(ref_xy, axis=0)
    seg_len2 = np.maximum((d**2).sum(axis=1), 1e-9)
    seg_len = np.sqrt(seg_len2)
    progress = np.full(len(xy), np.nan)
    offset = np.full(len(xy), np.nan)
    last = 0.0
    started = False
    for i, p in enumerate(xy):
        if started:
            lo = int(np.searchsorted(ref_cum, last - BACK_M, side="left"))
            hi = int(np.searchsorted(ref_cum, last + AHEAD_M, side="right"))
            lo, hi = max(lo - 1, 0), min(hi, len(a))
        else:
            lo, hi = 0, len(a)
        if hi <= lo:
            continue
        rel = p - a[lo:hi]
        tt = np.clip((rel * d[lo:hi]).sum(axis=1) / seg_len2[lo:hi], 0.0, 1.0)
        near = a[lo:hi] + tt[:, None] * d[lo:hi]
        dist = np.hypot(*(near - p).T)
        k = int(np.argmin(dist))
        offset[i] = dist[k]
        if dist[k] <= max_offset_m:
            pos = ref_cum[lo + k] + tt[k] * seg_len[lo + k]
            last = max(last, pos) if started else pos
            progress[i] = last
            started = True
    return progress, offset


def _times(df: pd.DataFrame, progress: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(course distance, elapsed time) of on-route samples, made non-decreasing."""
    on = ~np.isnan(progress)
    x = np.maximum.accumulate(progress[on])
    return x, df["t"].to_numpy()[on]


def _time_at(x: np.ndarray, t: np.ndarray, at: float) -> float:
    return first_reach(x, t, at)


def compare(conn: Conn, a: Activity, b: Activity, *, step_m: float = 1000.0) -> dict[str, Any]:
    """Align ``b`` to the course of ``a``; see the module docstring."""
    da = load_records(conn, a.id, ["lat", "lon", "alt", "dist", "speed", "hr"])
    db = load_records(conn, b.id, ["lat", "lon", "alt", "dist", "speed", "hr"])
    return compare_frames(a, da, b, db, step_m=step_m)


def compare_frames(
    a: Activity, da: pd.DataFrame, b: Activity, db: pd.DataFrame, *, step_m: float = 1000.0
) -> dict[str, Any]:
    if da["lat"].notna().sum() < 10 or db["lat"].notna().sum() < 10:
        raise ValueError("both activities need GPS positions")
    pos_a = da[da["lat"].notna()]
    origin = (float(pos_a["lat"].iloc[0]), float(pos_a["lon"].iloc[0]))
    ref_xy, ref_cum, ref_idx = reference_track(da, origin)
    length = float(ref_cum[-1])

    prog_a = np.full(len(da), np.nan)
    prog_a[ref_idx] = ref_cum
    prog_a = pd.Series(prog_a).where(da["lat"].notna()).interpolate(limit_area="inside").to_numpy()
    pos_b = db["lat"].notna() & db["lon"].notna()
    xy_b = _project(db.loc[pos_b, "lat"].to_numpy(), db.loc[pos_b, "lon"].to_numpy(), *origin)
    prog_b = np.full(len(db), np.nan)
    off_b = np.full(len(db), np.nan)
    p, o = project_progress(ref_xy, ref_cum, xy_b)
    prog_b[pos_b.to_numpy()] = p
    off_b[pos_b.to_numpy()] = o

    xa, ta = _times(da, prog_a)
    xb, tb = _times(db, prog_b)
    if xb.size < 10:
        raise ValueError("the second activity does not follow the first one's course")
    start = max(float(xa[0]), float(xb[0]))
    end = min(float(xa[-1]), float(xb[-1]))
    if end - start < 500:
        raise ValueError("less than 500 m of common course")
    on_route_b = float(np.mean(~np.isnan(prog_b[pos_b.to_numpy()])))
    t0a, t0b = _time_at(xa, ta, start), _time_at(xb, tb, start)

    def time_a(x: float) -> float:
        return _time_at(xa, ta, x) - t0a

    def time_b(x: float) -> float:
        return _time_at(xb, tb, x) - t0b

    # course altitude from the reference (smoothed), for per-section gain and climbs
    alt_a = (
        da["alt"].interpolate(limit_direction="both").rolling(15, center=True, min_periods=1).mean()
    )
    alt_course = alt_a.to_numpy()[ref_idx] if alt_a.notna().any() else np.full(len(ref_idx), np.nan)
    up, _ = ascent_profile(alt_course)

    def gain(x0: float, x1: float) -> float | None:
        if np.isnan(alt_course).all():
            return None
        return round(float(np.interp(x1, ref_cum, up) - np.interp(x0, ref_cum, up)), 1)

    def hr_between(
        df: pd.DataFrame, x: np.ndarray, prog: np.ndarray, x0: float, x1: float
    ) -> float | None:
        sel = (prog >= x0) & (prog < x1)
        return mean_or_none(df.loc[sel, "hr"], 0) if "hr" in df else None

    sections = []
    bounds = [start, *np.arange(math.floor(start / step_m + 1) * step_m, end, step_m).tolist(), end]
    cum_delta = 0.0
    for n, (x0, x1) in enumerate(pairwise(bounds), start=1):
        if x1 - x0 < 1:
            continue
        sa = time_a(x1) - time_a(x0)
        sb = time_b(x1) - time_b(x0)
        cum_delta = time_b(x1) - time_a(x1)
        sections.append(
            {
                "n": n,
                "from_km": round(x0 / 1000, 2),
                "to_km": round(x1 / 1000, 2),
                "gain_m": gain(x0, x1),
                "time_a_s": round(sa, 1),
                "time_b_s": round(sb, 1),
                "delta_s": round(sb - sa, 1),
                "cum_delta_s": round(cum_delta, 1),
                "hr_a": hr_between(da, xa, prog_a, x0, x1),
                "hr_b": hr_between(db, xb, prog_b, x0, x1),
            }
        )

    # climbs on the course (reference altitude), with both times
    climbs = []
    if not np.isnan(alt_course).all():
        pts = turning_points(alt_course, 10.0)
        for s, e in pairwise(pts):
            g = float(alt_course[e] - alt_course[s])
            x0, x1 = float(ref_cum[s]), float(ref_cum[e])
            if g < CLIMB_MIN_GAIN_M or x0 < start or x1 > end or x1 <= x0:
                continue
            sa = time_a(x1) - time_a(x0)
            sb = time_b(x1) - time_b(x0)
            climbs.append(
                {
                    "from_km": round(x0 / 1000, 2),
                    "to_km": round(x1 / 1000, 2),
                    "gain_m": round(g),
                    "grade_pct": round(g / (x1 - x0) * 100, 1),
                    "time_a_s": round(sa, 1),
                    "time_b_s": round(sb, 1),
                    "delta_s": round(sb - sa, 1),
                    "hr_a": hr_between(da, xa, prog_a, x0, x1),
                    "hr_b": hr_between(db, xb, prog_b, x0, x1),
                }
            )

    def stopped(df: pd.DataFrame, prog: np.ndarray) -> float:
        sel = (prog >= start) & (prog <= end)
        idx = np.flatnonzero(sel)
        if idx.size == 0:
            return 0.0
        part = df.iloc[idx[0] : idx[-1] + 1]
        return float(part.loc[part["still"] | part["paused"], "dt"].sum())

    total_a, total_b = time_a(end), time_b(end)
    grid = np.arange(start, end, 50.0)
    curve = [{"km": round(x / 1000, 3), "delta_s": round(time_b(x) - time_a(x), 1)} for x in grid]
    worst = sorted(sections, key=lambda s: s["delta_s"], reverse=True)[:3]
    return {
        "a": a.as_dict(),
        "b": b.as_dict(),
        "course_length_m": round(length),
        "common": {"from_km": round(start / 1000, 2), "to_km": round(end / 1000, 2)},
        "on_route_b": round(on_route_b, 2),
        "time_a_s": round(total_a, 1),
        "time_b_s": round(total_b, 1),
        "delta_s": round(total_b - total_a, 1),
        "hr_a": hr_between(da, xa, prog_a, start, end),
        "hr_b": hr_between(db, xb, prog_b, start, end),
        "stopped_a_s": round(stopped(da, prog_a)),
        "stopped_b_s": round(stopped(db, prog_b)),
        "sections": sections,
        "most_lost": [s["n"] for s in worst if s["delta_s"] > 0],
        "climbs": climbs,
        "curve": curve,
        "_tracks": {"a": (xa, ta - t0a), "b": (xb, tb - t0b)},
        "_profile": (ref_cum, alt_course),
    }


# Finding activities on the same course --------------------------------------------------------


def _cells(xy: np.ndarray, cell: float) -> set[tuple[int, int]]:
    keys = np.floor(xy / cell).astype(np.int64)
    return {(int(i), int(j)) for i, j in keys}


def _covered(xy: np.ndarray, cells: set[tuple[int, int]], cell: float) -> float:
    if len(xy) == 0:
        return 0.0
    keys = np.floor(xy / cell).astype(np.int64)
    hits = 0
    for i, j in keys:
        if any((i + di, j + dj) in cells for di in (-1, 0, 1) for dj in (-1, 0, 1)):
            hits += 1
    return hits / len(keys)


def _sample_xy(conn: Conn, activity_id: int, origin: tuple[float, float]) -> np.ndarray:
    rows = conn.execute(
        "SELECT lat, lon FROM activity_records WHERE activity_id = %s AND lat IS NOT NULL"
        " AND extract(epoch FROM ts)::bigint %% 5 = 0 ORDER BY ts",
        (activity_id,),
    ).fetchall()
    if not rows:
        return np.zeros((0, 2))
    arr = np.array([[r["lat"], r["lon"]] if isinstance(r, dict) else list(r) for r in rows])
    return _project(arr[:, 0], arr[:, 1], *origin)


def find_same_course(
    conn: Conn,
    ref: Activity,
    *,
    min_coverage: float = 0.8,
    limit: int = 10,
    all_types: bool = False,
) -> list[dict[str, Any]]:
    """Other activities that cover the reference's course.

    ``coverage`` = share of the reference track (sampled every 5 s) that lies within about
    :data:`COVER_CELL_M` of the candidate's track; ``on_course`` = the reverse share (low when
    the candidate is much longer or wanders off). Candidates are first narrowed down to
    activities whose bounding box overlaps the reference's.
    """
    box = conn.execute(
        "SELECT min(lat) AS lat0, max(lat) AS lat1, min(lon) AS lon0, max(lon) AS lon1"
        " FROM activity_records WHERE activity_id = %s",
        (ref.id,),
    ).fetchone()
    box = dict(box) if not isinstance(box, dict) else box
    if box["lat0"] is None:
        raise ValueError("the reference activity has no GPS positions")
    origin = (float(box["lat0"]), float(box["lon0"]))
    margin = 0.002
    rows = conn.execute(
        """
        SELECT activity_id FROM activity_records
        WHERE activity_id <> %s AND lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        GROUP BY activity_id HAVING count(*) > 60
        """,
        (
            ref.id,
            box["lat0"] - margin,
            box["lat1"] + margin,
            box["lon0"] - margin,
            box["lon1"] + margin,
        ),
    ).fetchall()
    candidate_ids = {r["activity_id"] if isinstance(r, dict) else r[0] for r in rows}
    if not candidate_ids:
        return []
    by_id = {a.id: a for a in find_activities(conn, runs_only=not all_types, with_records=True)}
    ref_xy = _sample_xy(conn, ref.id, origin)
    ref_cells = _cells(ref_xy, COVER_CELL_M)
    out = []
    for activity_id in candidate_ids:
        activity = by_id.get(activity_id)
        if activity is None:
            continue
        xy = _sample_xy(conn, activity_id, origin)
        if len(xy) == 0:
            continue
        coverage = _covered(ref_xy, _cells(xy, COVER_CELL_M), COVER_CELL_M)
        if coverage < min_coverage:
            continue
        on_course = _covered(xy, ref_cells, COVER_CELL_M)
        out.append(
            {
                "activity_id": activity_id,
                "date": activity.local_date,
                "name": activity.name,
                "distance_km": rounded(
                    activity.distance_m / 1000 if activity.distance_m else None, 2
                ),
                "coverage": round(coverage, 2),
                "on_course": round(on_course, 2),
            }
        )
    out.sort(key=lambda r: (r["coverage"], r["on_course"]), reverse=True)
    return out[:limit]
