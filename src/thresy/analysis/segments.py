"""Fixed segments (a start and an end point) and their efforts across all activities.

Segments are athlete-specific, so their definitions live in the database (table ``segments``),
loaded from a small file the athlete keeps outside git (:func:`load_segment_file`).

Matching (:func:`match_segment`) is a port of the old bot's matcher, applied to database
records: an effort starts at the last sample within ``tol_m`` of the start point before the
first sample within ``tol_m`` of the end point, reached within ``max_s`` seconds. It counts when
its distance lies strictly between ``dist_min_m`` and ``dist_max_m``, it lasts more than 30 s,
and (for ``uphill`` segments) it ends higher than it starts. Distances to the points use an
equirectangular approximation, as the old matcher did. Durations are wall-clock time.

Efforts are cached in ``segment_efforts``. Candidate activities are found with one bounding-box
query over the records (activities that pass near both points); activities that already have
cached efforts are not scanned again unless ``refresh`` is set. Changing a segment's definition
with :func:`upsert_segments` clears its cached efforts. On a connection that may not write, the
efforts are computed without the cache.
"""

from __future__ import annotations

import json
import logging
import math
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg

from thresy.analysis.data import Activity, Conn, get_activity, load_records
from thresy.analysis.fmt import rounded

log = logging.getLogger(__name__)

M_PER_DEG = 111320.0
MIN_EFFORT_S = 30.0
WRITE_DENIED = (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)


@dataclass(frozen=True)
class SegmentDef:
    key: str
    name: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    tol_m: float = 30.0
    dist_min_m: float | None = None
    dist_max_m: float | None = None
    max_s: float = 1800.0
    uphill: bool = True
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = tuple(f.name for f in fields(SegmentDef))


def segment_from_mapping(item: Mapping[str, Any]) -> SegmentDef:
    """A definition from a file entry. ``start``/``end`` may be ``[lat, lon]`` pairs."""
    data = dict(item)
    for point in ("start", "end"):
        if point in data:
            value = data.pop(point)
            if not isinstance(value, list | tuple) or len(value) != 2:
                raise ValueError(f"{point} must be [lat, lon]")
            data[f"{point}_lat"], data[f"{point}_lon"] = float(value[0]), float(value[1])
    unknown = set(data) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    missing = {"key", "start_lat", "start_lon", "end_lat", "end_lon"} - set(data)
    if missing:
        raise ValueError(f"missing field(s): {', '.join(sorted(missing))}")
    data.setdefault("name", data["key"])
    seg = SegmentDef(**data)
    for name in ("start_lat", "end_lat"):
        if not -90 <= getattr(seg, name) <= 90:
            raise ValueError(f"{name} out of range")
    for name in ("start_lon", "end_lon"):
        if not -180 <= getattr(seg, name) <= 180:
            raise ValueError(f"{name} out of range")
    if seg.tol_m <= 0 or seg.max_s <= 0:
        raise ValueError("tol_m and max_s must be positive")
    return seg


def load_segment_file(path: Path) -> list[SegmentDef]:
    """Read segment definitions from a TOML file (``[[segments]]`` tables) or a JSON file (a
    list, or ``{"segments": [...]}``)."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".toml":
        data: Any = tomllib.loads(text)
    else:
        data = json.loads(text)
    items = data.get("segments", []) if isinstance(data, Mapping) else data
    if not isinstance(items, list) or not items:
        raise ValueError("no segments found (expected a list or a 'segments' list)")
    out = []
    for n, item in enumerate(items, start=1):
        try:
            out.append(segment_from_mapping(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"segment {n}: {exc}") from exc
    keys = [s.key for s in out]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate segment keys")
    return out


def list_segments(conn: Conn) -> list[SegmentDef]:
    rows = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM segments ORDER BY key").fetchall()
    return [SegmentDef(**_row(r)) for r in rows]


def get_segment(conn: Conn, key: str) -> SegmentDef | None:
    row = conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM segments WHERE key = %s", (key,)
    ).fetchone()
    return None if row is None else SegmentDef(**_row(row))


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if isinstance(row, Mapping) else dict(zip(_FIELDS, row, strict=True))


def upsert_segments(conn: Conn, segments: Iterable[SegmentDef]) -> dict[str, int]:
    """Insert or update definitions by key. A changed definition clears its cached efforts."""
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with conn.transaction():
        for seg in segments:
            old = get_segment(conn, seg.key)
            if old == seg:
                counts["unchanged"] += 1
                continue
            values = seg.as_dict()
            conn.execute(
                f"INSERT INTO segments ({', '.join(_FIELDS)})"
                f" VALUES ({', '.join(f'%({f})s' for f in _FIELDS)})"
                " ON CONFLICT (key) DO UPDATE SET "
                + ", ".join(f"{f} = EXCLUDED.{f}" for f in _FIELDS if f != "key"),
                values,
            )
            if old is None:
                counts["new"] += 1
            else:
                conn.execute("DELETE FROM segment_efforts WHERE segment_key = %s", (seg.key,))
                counts["updated"] += 1
    return counts


# Matching -------------------------------------------------------------------------------------


def point_distance_m(lat: np.ndarray, lon: np.ndarray, point: tuple[float, float]) -> np.ndarray:
    """Equirectangular distance in metres from each (lat, lon) to ``point``."""
    plat, plon = point
    dy = (np.asarray(lat) - plat) * M_PER_DEG
    dx = (np.asarray(lon) - plon) * M_PER_DEG * math.cos(math.radians(plat))
    return np.hypot(dx, dy)


def match_segment(df: pd.DataFrame, seg: SegmentDef) -> list[dict[str, Any]]:
    """Efforts on ``seg`` in one activity's record frame (see the module docstring).

    Each effort has ``start_utc``, ``duration_s``, ``dist_m``, ``gain_m`` (altitude at the end
    minus at the start), ``avg_hr``, ``max_hr``, ``hr_last60`` (mean over the last 60 s),
    ``pace_s_per_km`` and ``m_per_beat`` (distance / (avg HR * minutes)).
    """
    if df.empty or "lat" not in df:
        return []
    d = df[df["lat"].notna() & df["lon"].notna()].reset_index(drop=True)
    if d.empty:
        return []
    lat, lon = d["lat"].to_numpy(), d["lon"].to_numpy()
    ds = point_distance_m(lat, lon, (seg.start_lat, seg.start_lon))
    de = point_distance_m(lat, lon, (seg.end_lat, seg.end_lon))
    t = d["t"].to_numpy(dtype="float64")
    dist = d["dist"].to_numpy(dtype="float64") if "dist" in d else np.full(len(d), np.nan)
    alt = d["alt"].to_numpy(dtype="float64") if "alt" in d else np.full(len(d), np.nan)
    hr = d["hr"].to_numpy(dtype="float64") if "hr" in d else np.full(len(d), np.nan)
    dmin = seg.dist_min_m if seg.dist_min_m is not None else 0.0
    dmax = seg.dist_max_m if seg.dist_max_m is not None else 1e9
    tol = seg.tol_m
    near_start = ds < tol
    near_end = de < tol
    n = len(d)
    out = []
    k = 0
    while k < n:
        if not near_start[k]:
            nxt = np.flatnonzero(near_start[k:])
            if nxt.size == 0:
                break
            k += int(nxt[0])
        limit = int(np.searchsorted(t, t[k] + seg.max_s, side="left"))
        ends = np.flatnonzero(near_end[k:limit])
        j = k + int(ends[0]) if ends.size else limit
        if j >= n or not near_end[j]:
            k += 1
            continue
        starts = np.flatnonzero(near_start[k:j])
        ls = k + int(starts[-1]) if starts.size else k
        dur = t[j] - t[ls]
        dm = dist[j] - dist[ls]
        gain = alt[j] - alt[ls]
        if dmin < dm < dmax and dur > MIN_EFFORT_S and (gain > 0 or not seg.uphill):
            g_hr = hr[ls : j + 1]
            g_hr = g_hr[~np.isnan(g_hr)]
            last = hr[ls : j + 1][t[ls : j + 1] >= t[j] - 60]
            last = last[~np.isnan(last)]
            avg_hr = float(g_hr.mean()) if g_hr.size else None
            out.append(
                {
                    "start_utc": pd.Timestamp(d["ts"].iloc[ls]).to_pydatetime(),
                    "duration_s": float(dur),
                    "dist_m": float(dm),
                    "gain_m": None if math.isnan(gain) else float(gain),
                    "avg_hr": avg_hr,
                    "max_hr": float(g_hr.max()) if g_hr.size else None,
                    "hr_last60": float(last.mean()) if last.size else None,
                    "pace_s_per_km": float(dur / dm * 1000) if dm else None,
                }
            )
        k = j + 1
    return out


def with_derived(effort: dict[str, Any]) -> dict[str, Any]:
    out = dict(effort)
    hr, dur, dm = out.get("avg_hr"), out.get("duration_s"), out.get("dist_m")
    out["m_per_beat"] = round(dm / (hr * dur / 60), 3) if hr and dur and dm else None
    return out


# Candidates and the cache ---------------------------------------------------------------------


def _bbox(lat: float, lon: float, tol_m: float) -> tuple[float, float, float, float]:
    dlat = tol_m / M_PER_DEG
    dlon = tol_m / (M_PER_DEG * max(math.cos(math.radians(lat)), 1e-6))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def candidate_activities(conn: Conn, seg: SegmentDef) -> list[int]:
    """Activities with records near both the start and the end point (bounding boxes)."""
    s = _bbox(seg.start_lat, seg.start_lon, seg.tol_m)
    e = _bbox(seg.end_lat, seg.end_lon, seg.tol_m)
    rows = conn.execute(
        """
        SELECT activity_id FROM activity_records
        WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        INTERSECT
        SELECT activity_id FROM activity_records
        WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
        """,
        (*s, *e),
    ).fetchall()
    return sorted(_first(r) for r in rows)


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, Mapping) else row[0]


_EFFORT_COLS = (
    "start_utc",
    "duration_s",
    "dist_m",
    "gain_m",
    "avg_hr",
    "max_hr",
    "hr_last60",
    "pace_s_per_km",
)


def cached_efforts(conn: Conn, key: str) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        f"SELECT activity_id, {', '.join(_EFFORT_COLS)} FROM segment_efforts"
        " WHERE segment_key = %s ORDER BY start_utc",
        (key,),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        row = _row_efforts(r)
        out.setdefault(int(row.pop("activity_id")), []).append(row)
    return out


def _row_efforts(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(("activity_id", *_EFFORT_COLS), row, strict=True))


def _write_cache(
    conn: Conn, key: str, found: dict[int, list[dict[str, Any]]], *, replace: bool
) -> None:
    with conn.transaction():
        if replace:
            conn.execute("DELETE FROM segment_efforts WHERE segment_key = %s", (key,))
        rows = [
            {"segment_key": key, "activity_id": aid, **{c: e[c] for c in _EFFORT_COLS}}
            for aid, efforts in found.items()
            for e in efforts
        ]
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO segment_efforts (segment_key, activity_id, "
                    + ", ".join(_EFFORT_COLS)
                    + ") VALUES (%(segment_key)s, %(activity_id)s, "
                    + ", ".join(f"%({c})s" for c in _EFFORT_COLS)
                    + ") ON CONFLICT (segment_key, activity_id, start_utc) DO UPDATE SET "
                    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _EFFORT_COLS if c != "start_utc"),
                    rows,
                )


def segment_efforts(
    conn: Conn, seg: SegmentDef, *, refresh: bool = False, use_cache: bool = True
) -> dict[str, Any]:
    """All efforts on ``seg``, using and filling the cache where possible.

    Returns ``{"efforts": [...], "scanned": n, "cached": n, "cache": "used"|"written"|
    "read-only"|"off"}``; each effort carries ``activity_id``.
    """
    candidates = candidate_activities(conn, seg)
    cached = cached_efforts(conn, seg.key) if use_cache and not refresh else {}
    to_scan = [a for a in candidates if a not in cached]
    found: dict[int, list[dict[str, Any]]] = {}
    for activity_id in to_scan:
        df = load_records(conn, activity_id, ["lat", "lon", "alt", "dist", "hr"])
        efforts = match_segment(df, seg)
        if efforts:
            found[activity_id] = efforts
    status = "off" if not use_cache else "used"
    if use_cache and (found or refresh):
        try:
            _write_cache(conn, seg.key, found, replace=refresh)
            status = "written"
        except WRITE_DENIED:
            status = "read-only"
            log.info("segment cache not written: the connection may not write")
    efforts = []
    for activity_id, items in {**cached, **found}.items():
        if activity_id not in candidates:
            continue
        for e in items:
            efforts.append({"activity_id": activity_id, **e})
    efforts.sort(key=lambda e: e["start_utc"])
    return {
        "efforts": efforts,
        "candidates": len(candidates),
        "scanned": len(to_scan),
        "cached": len([a for a in candidates if a in cached]),
        "cache": status,
    }


def match_activity(conn: Conn, activity: Activity, segments: Iterable[SegmentDef]) -> list[dict]:
    """Efforts of one activity on each of ``segments`` (computed, not cached)."""
    df = load_records(conn, activity.id, ["lat", "lon", "alt", "dist", "hr"])
    out = []
    for seg in segments:
        for effort in match_segment(df, seg):
            out.append({"segment_key": seg.key, "segment": seg.name, **with_derived(effort)})
    return out


# History --------------------------------------------------------------------------------------


def history(
    conn: Conn,
    seg: SegmentDef,
    efforts: list[dict[str, Any]],
    *,
    at_hr: float | None = None,
    hr_band: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Efforts in date order with local times, the best effort, per-year summaries and a
    pace-at-heart-rate view.

    Pace at heart rate: a straight line pace = a + b * avg_hr is fitted to all efforts with a
    heart rate (needs at least 5 efforts spanning 8 bpm, and b < 0, i.e. faster at higher heart
    rate). Each effort's ``pace_at_hr`` is its pace moved along that slope to ``at_hr`` (default:
    the median heart rate of the efforts), so efforts run at different intensities can be
    compared. ``m_per_beat`` (distance per heartbeat) is a fit-free alternative.
    """
    activities: dict[int, Activity | None] = {}
    rows = []
    for e in efforts:
        aid = e["activity_id"]
        if aid not in activities:
            activities[aid] = get_activity(conn, aid)
        activity = activities[aid]
        row = with_derived(e)
        row["start_local"] = activity.local(e["start_utc"]) if activity else None
        row["activity_name"] = activity.name if activity else None
        rows.append(row)
    if hr_band is not None:
        lo, hi = hr_band
        rows = [r for r in rows if r["avg_hr"] is not None and lo <= r["avg_hr"] <= hi]
    fit = _pace_hr_fit(rows)
    ref_hr = at_hr
    if fit is not None:
        if ref_hr is None:
            ref_hr = float(np.median([r["avg_hr"] for r in rows if r["avg_hr"] is not None]))
        slope = fit["slope_s_per_bpm"]
        for r in rows:
            if r["avg_hr"] is not None and r["pace_s_per_km"] is not None:
                r["pace_at_hr"] = round(r["pace_s_per_km"] + slope * (ref_hr - r["avg_hr"]), 1)
    for r in rows:
        r.setdefault("pace_at_hr", None)
    best = min(rows, key=lambda r: r["duration_s"]) if rows else None
    years: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        if r["start_local"] is not None:
            years.setdefault(r["start_local"].year, []).append(r)
    per_year = [
        {
            "year": y,
            "efforts": len(items),
            "best_s": min(i["duration_s"] for i in items),
            "median_s": float(np.median([i["duration_s"] for i in items])),
            "avg_hr": rounded(
                np.mean([i["avg_hr"] for i in items if i["avg_hr"] is not None])
                if any(i["avg_hr"] is not None for i in items)
                else None
            ),
            "median_pace_at_hr": rounded(
                float(np.median([i["pace_at_hr"] for i in items if i["pace_at_hr"] is not None]))
                if any(i["pace_at_hr"] is not None for i in items)
                else None
            ),
        }
        for y, items in sorted(years.items())
    ]
    return {
        "segment": seg.as_dict(),
        "count": len(rows),
        "activities": len({r["activity_id"] for r in rows}),
        "best": best,
        "fit": fit,
        "at_hr": rounded(ref_hr) if fit is not None else None,
        "hr_band": list(hr_band) if hr_band else None,
        "per_year": per_year,
        "efforts": rows,
    }


def _pace_hr_fit(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    pts = [
        (r["avg_hr"], r["pace_s_per_km"])
        for r in rows
        if r["avg_hr"] is not None and r["pace_s_per_km"] is not None
    ]
    if len(pts) < 5:
        return None
    hr = np.array([p[0] for p in pts])
    pace = np.array([p[1] for p in pts])
    if hr.max() - hr.min() < 8:
        return None
    slope, intercept = np.polyfit(hr, pace, 1)
    if slope >= 0:
        return None
    pred = intercept + slope * hr
    ss_res = float(((pace - pred) ** 2).sum())
    ss_tot = float(((pace - pace.mean()) ** 2).sum())
    return {
        "slope_s_per_bpm": round(float(slope), 2),
        "intercept": round(float(intercept), 1),
        "r2": round(1 - ss_res / ss_tot, 2) if ss_tot > 0 else None,
        "n": len(pts),
    }
