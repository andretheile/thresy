"""What ``coach route`` reports about a route: numbers, and one line of words built from them.

Every measure is computed on the route resampled every :data:`STEP_M` metres:

* **distance**: the length of the track itself (the provider's own figure is kept apart);
* **ascent and descent**: from the elevation profile, smoothed over :data:`ELE_SMOOTH_M` and
  counted with a dead band of :data:`ASCENT_DEAD_BAND_M` (``thresy.analysis.data.
  ascent_profile``); missing when the route has no elevation, never estimated;
* **familiar**: the share of the route on ground the athlete's recorded runs passed before
  (grid cells, ``thresy.tools.route_history``);
* **retraced**: the share of the route on ground the route itself covers twice: a point counts
  when the route passes within :data:`RETRACE_TOL_M` of it again at least
  :data:`RETRACE_GAP_M` further along. Both passes count, so a pure there-and-back is 100 % and
  a lollipop with a 2 km stick on a 10 km route about 40 %. Two paths closer together than the
  tolerance (switchbacks) count as retraced too;
* **segments passed**: a stored segment is passed when the route comes within its tolerance
  (at least :data:`SEGMENT_TOL_M`) of its start and then of its end, with the distance between
  within the segment's range (10 % slack); passing end first is reported as ``reverse``;
* **surface and way type**: the provider's shares, when it gives them.

The words (:func:`summary`) use fixed thresholds, listed in the constants below and in
``coach route --help``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from thresy.tools.route import Resampled, Track, distance_m, resample

STEP_M = 10.0
ELE_SMOOTH_M = 70.0
ASCENT_DEAD_BAND_M = 2.0
RETRACE_TOL_M = 20.0
RETRACE_GAP_M = 100.0
SEGMENT_TOL_M = 30.0
LOOP_GAP_M = 200.0

# Words: thresholds
TARGET_TOL_KM = 0.25
TARGET_TOL_SHARE = 0.025
SHAPE_LEVELS = (0.15, 0.5, 0.85)  # retraced share: loop / out-and-back parts / mostly / all
HILL_LEVELS_M_PER_KM = (5.0, 12.0, 25.0)
HILL_WORDS = ("flat", "gently rolling", "hilly", "very hilly")
FAMILIAR_LEVELS = (0.1, 0.4, 0.75)
PAVED_MOSTLY = 0.75


def smoothed_elevation(res: Resampled) -> np.ndarray | None:
    if res.ele is None:
        return None
    window = max(round(ELE_SMOOTH_M / STEP_M), 1)
    if window <= 1 or len(res.ele) < window:
        return res.ele.copy()
    kernel = np.ones(window) / window
    padded = np.pad(res.ele, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def ascent_descent(res: Resampled) -> tuple[float | None, float | None, np.ndarray | None]:
    """Ascent, descent and the cumulative ascent per point (``None`` without elevation)."""
    from thresy.analysis.data import ascent_profile

    ele = smoothed_elevation(res)
    if ele is None:
        return None, None, None
    up, down = ascent_profile(ele, ASCENT_DEAD_BAND_M)
    return float(up[-1]), float(down[-1]), up


def retraced(res: Resampled) -> np.ndarray:
    """Per point: the route passes here again (see the module docstring)."""
    xy, cum = res.xy, res.cum
    n = len(xy)
    out = np.zeros(n, dtype=bool)
    keys = np.floor(xy / RETRACE_TOL_M).astype(np.int64).tolist()
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(keys):
        buckets.setdefault((x, y), []).append(i)
    for i, (x, y) in enumerate(keys):
        near = [
            j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in buckets.get((x + dx, y + dy), ())
        ]
        if len(near) < 2:
            continue
        idx = np.array(near)
        far = idx[np.abs(cum[idx] - cum[i]) >= RETRACE_GAP_M]
        if far.size and (np.hypot(*(xy[far] - xy[i]).T) <= RETRACE_TOL_M).any():
            out[i] = True
    return out


def _distances(res: Resampled, point: tuple[float, float]) -> np.ndarray:
    lat0 = res.origin[0]
    dy = (res.lat - point[0]) * 111320.0
    dx = (res.lon - point[1]) * 111320.0 * math.cos(math.radians(lat0))
    return np.hypot(dx, dy)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous ``True`` stretches as (first, last) indices."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def segment_passes(res: Resampled, segments: list[Any]) -> list[dict]:
    """Stored segments the route passes, in route order (see the module docstring)."""
    ele = smoothed_elevation(res)
    out = []
    for seg in segments:
        start = (seg.start_lat, seg.start_lon)
        end = (seg.end_lat, seg.end_lon)
        tol = max(float(seg.tol_m or 0), SEGMENT_TOL_M)
        straight = distance_m(start, end)
        lo = (seg.dist_min_m or 0.0) * 0.9
        hi = (seg.dist_max_m * 1.1) if seg.dist_max_m else straight * 2 + 500.0
        for direction, a, b in (("forward", start, end), ("reverse", end, start)):
            da, db = _distances(res, a), _distances(res, b)
            near_a, near_b = _runs(da < tol), _runs(db < tol)
            used_until = -1
            for b0, b1 in near_b:
                # the last pass of the start before this pass of the end
                before = [r for r in near_a if r[0] > used_until and r[1] < b0]
                if not before:
                    continue
                a0, a1 = before[-1]
                i = a0 + int(np.argmin(da[a0 : a1 + 1]))
                j = b0 + int(np.argmin(db[b0 : b1 + 1]))
                length = float(res.cum[j] - res.cum[i])
                if not lo <= length <= hi:
                    continue
                gain = None if ele is None else round(float(ele[j] - ele[i]))
                out.append(
                    {
                        "key": seg.key,
                        "name": seg.name,
                        "direction": direction,
                        "uphill": bool(seg.uphill),
                        "from_km": round(float(res.cum[i]) / 1000, 2),
                        "to_km": round(float(res.cum[j]) / 1000, 2),
                        "length_km": round(length / 1000, 2),
                        "gain_m": gain,
                    }
                )
                used_until = b1
    out.sort(key=lambda p: (p["from_km"], p["to_km"]))
    return out


def _share(mask: np.ndarray) -> float:
    return float(mask.mean()) if mask.size else 0.0


def surface_groups(surface: dict[str, float] | None) -> dict[str, float] | None:
    """Paved, unpaved and unknown shares from the provider's surface shares."""
    if not surface:
        return None
    from thresy.tools.route_provider import PAVED, SURFACE, UNPAVED

    paved = {SURFACE[c] for c in PAVED}
    unpaved = {SURFACE[c] for c in UNPAVED}
    out = {"paved": 0.0, "unpaved": 0.0, "unknown": 0.0}
    for name, value in surface.items():
        key = "paved" if name in paved else "unpaved" if name in unpaved else "unknown"
        out[key] += value
    return {k: round(v, 3) for k, v in out.items()}


def measure(
    track: Track,
    *,
    known: Any | None,
    segments: list[Any],
    target_m: float | None = None,
    surface: dict[str, float] | None = None,
    waytype: dict[str, float] | None = None,
    elevation_source: str | None = None,
) -> dict[str, Any]:
    """The report of one route (see the module docstring); ``summary`` holds the words."""
    res = resample(track, STEP_M)
    length = float(res.cum[-1])
    up, down, _ = ascent_descent(res)
    ele = smoothed_elevation(res)
    gap = distance_m(track.start, track.end)
    loop = gap <= LOOP_GAP_M
    again = retraced(res)
    report: dict[str, Any] = {
        "distance_km": round(length / 1000, 2),
        "loop": loop,
        "start": [round(track.start[0], 6), round(track.start[1], 6)],
        "end": [round(track.end[0], 6), round(track.end[1], 6)],
        "start_to_end_m": round(gap),
        "ascent_m": None if up is None else round(up),
        "descent_m": None if down is None else round(down),
        "min_ele_m": None if ele is None else round(float(np.min(ele))),
        "max_ele_m": None if ele is None else round(float(np.max(ele))),
        "elevation": elevation_source if up is not None else None,
        "retraced_share": round(_share(again), 3),
        "retraced_km": round(_share(again) * length / 1000, 2),
    }
    if target_m:
        off = length - target_m
        report["target_km"] = round(target_m / 1000, 2)
        report["off_target_km"] = round(off / 1000, 2)
        report["off_target_pct"] = round(off / target_m * 100, 1)
        report["close_to_target"] = abs(off) <= target_tolerance_m(target_m)
    if known is not None:
        familiar = known.familiar(res.lat, res.lon)
        report["familiar_share"] = round(_share(familiar), 3)
        report["familiar_km"] = round(_share(familiar) * length / 1000, 2)
        report["known_cells"] = len(known.cells)
        report["_familiar"] = familiar
    report["surface"] = surface_groups(surface)
    report["surface_detail"] = {k: round(v, 3) for k, v in surface.items()} if surface else None
    report["waytype"] = {k: round(v, 3) for k, v in waytype.items()} if waytype else None
    report["segments"] = segment_passes(res, segments)
    report["segments_known"] = len(segments)
    report["_res"] = res
    report["_retraced"] = again
    report["summary"] = summary(report)
    return report


def target_tolerance_m(target_m: float) -> float:
    return max(TARGET_TOL_KM * 1000, TARGET_TOL_SHARE * target_m)


# Words ----------------------------------------------------------------------------------------


def signed_km(value: float) -> str:
    """``0.23`` -> ``+0.2 km``; never ``-0.0``."""
    return f"{round(value, 1) + 0.0:+.1f} km"


def _pct(share: float | None) -> str:
    return "-" if share is None else f"{share * 100:.0f}%"


def shape_words(report: dict[str, Any]) -> str:
    r = report["retraced_share"]
    level = sum(r >= limit for limit in SHAPE_LEVELS)
    if report["loop"]:
        words = (
            "a loop",
            "a loop with out-and-back parts",
            "mostly out and back on the same way",
            "there and back on the same way",
        )[level]
    else:
        words = (
            "point to point",
            "point to point with out-and-back parts",
            "point to point, mostly retracing itself",
            "point to point, retracing itself",
        )[level]
    return words if level == 0 else f"{words} ({_pct(r)} retraced)"


def distance_words(report: dict[str, Any]) -> str:
    text = f"{report['distance_km']:.1f} km"
    if "target_km" not in report:
        return text
    off = report["off_target_km"]
    if report["close_to_target"]:
        return f"{text}, close to the {report['target_km']:g} km target ({signed_km(off)})"
    side = "short of" if off < 0 else "over"
    return f"{text}, {abs(off):.1f} km {side} the {report['target_km']:g} km target"


def hill_words(report: dict[str, Any]) -> str:
    up = report["ascent_m"]
    if up is None:
        return "ascent unknown (no elevation)"
    per_km = up / max(report["distance_km"], 0.1)
    level = sum(per_km >= limit for limit in HILL_LEVELS_M_PER_KM)
    return f"{HILL_WORDS[level]} ({up} m up)"


def familiar_words(report: dict[str, Any]) -> str | None:
    if "familiar_share" not in report:
        return None
    if not report.get("known_cells"):
        return "new ground (no recorded runs in this area)"
    f = report["familiar_share"]
    level = sum(f >= limit for limit in FAMILIAR_LEVELS)
    words = (
        "new ground",
        "mostly new ground",
        "partly on ground run before",
        "mostly on ground run before",
    )[level]
    return f"{words} ({_pct(f)} run before)"


def segment_words(report: dict[str, Any]) -> str | None:
    passes = report["segments"]
    if not passes:
        return "passes no stored segment" if report["segments_known"] else None
    bits = []
    for p in passes:
        where = f"km {p['from_km']:.1f}-{p['to_km']:.1f}"
        if p["direction"] == "reverse":
            how = "in reverse, downhill" if p["uphill"] else "in reverse"
            bits.append(f"{p['name']} {how} ({where})")
        else:
            bits.append(f"{p['name']} ({where})")
    return "passes " + ", ".join(bits)


def surface_words(report: dict[str, Any]) -> str | None:
    groups = report.get("surface")
    if not groups:
        return None
    paved, unpaved, unknown = groups["paved"], groups["unpaved"], groups["unknown"]
    numbers = f"paved {_pct(paved)}, unpaved {_pct(unpaved)}" + (
        f", unknown {_pct(unknown)}" if unknown >= 0.05 else ""
    )
    if unknown >= 0.4:
        word = "surface mostly unknown"
    elif paved >= PAVED_MOSTLY:
        word = "mostly paved"
    elif unpaved >= PAVED_MOSTLY:
        word = "mostly unpaved"
    else:
        word = "mixed surface"
    return f"{word} ({numbers})"


def summary(report: dict[str, Any]) -> str:
    """One line of words from the numbers, e.g. ``a loop, 10.2 km, close to the 10 km target
    (+0.2 km); hilly (85 m up); mostly on ground run before (78% run before); passes Hill
    (km 3.1-4.3); mostly paved (paved 80%, unpaved 20%)``."""
    parts = [f"{shape_words(report)}, {distance_words(report)}", hill_words(report)]
    for words in (familiar_words(report), segment_words(report), surface_words(report)):
        if words:
            parts.append(words)
    return "; ".join(parts)


# Text -----------------------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip()]
    for r in rows:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)).rstrip())
    return lines


def updown(report: dict[str, Any]) -> str:
    if report["ascent_m"] is None:
        return "-"
    return f"{report['ascent_m']}/{report['descent_m']}"


def surface_cell(report: dict[str, Any]) -> str:
    groups = report.get("surface")
    if not groups:
        return "-"
    return f"{_pct(groups['paved'])}/{_pct(groups['unpaved'])}/{_pct(groups['unknown'])}"


def segment_line(p: dict[str, Any]) -> str:
    how = ""
    if p["direction"] == "reverse":
        how = " in reverse (downhill)" if p["uphill"] else " in reverse"
    gain = "" if p["gain_m"] is None else f", {p['gain_m']:+d} m"
    return (
        f"km {p['from_km']:.2f}-{p['to_km']:.2f} {p['name']} ({p['key']}){how},"
        f" {p['length_km']:.2f} km{gain}"
    )


def candidates_text(candidates: list[dict[str, Any]]) -> list[str]:
    """The words, the table and the segments of several routes."""
    lines = ["In words (from the numbers below):"]
    for c in candidates:
        lines.append(f"{c['n']}. {c['file_name']}: {c['summary']}")
    with_target = any("target_km" in c for c in candidates)
    header = ["#", "km"] + (["vs target"] if with_target else [])
    header += ["up/down m", "familiar", "retraced", "paved/unpaved/unknown"]
    rows = []
    for c in candidates:
        row = [str(c["n"]), f"{c['distance_km']:.1f}"]
        if with_target:
            row.append(signed_km(c["off_target_km"]) if "target_km" in c else "-")
        row += [updown(c), _pct(c.get("familiar_share")), _pct(c["retraced_share"])]
        row.append(surface_cell(c))
        rows.append(row)
    lines += ["", *_table(header, rows)]
    if any(c["segments_known"] for c in candidates):
        lines += ["", "Stored segments passed, in route order:"]
        for c in candidates:
            if not c["segments"]:
                lines.append(f"{c['n']}. none")
            for k, p in enumerate(c["segments"]):
                lines.append(f"{c['n'] if k == 0 else ' '}. {segment_line(p)}")
    ways = [c for c in candidates if c.get("waytype")]
    if ways:
        lines += ["", "Way types (share of distance):"]
        for c in ways:
            parts = sorted(c["waytype"].items(), key=lambda kv: -kv[1])
            text = ", ".join(f"{name} {_pct(v)}" for name, v in parts if v >= 0.02)
            lines.append(f"{c['n']}. {text}")
    return lines


def single_text(report: dict[str, Any]) -> list[str]:
    """The details of one route, for ``show`` and ``export``."""
    lines = [f"In words: {report['summary']}", ""]
    lines.append(f"distance: {report['distance_km']:.2f} km")
    if report["ascent_m"] is None:
        lines.append("ascent and descent: unknown (the route has no elevation)")
    else:
        source = f" (from {report['elevation']})" if report.get("elevation") else ""
        lines.append(
            f"ascent {report['ascent_m']} m, descent {report['descent_m']} m{source}; elevation"
            f" {report['min_ele_m']}-{report['max_ele_m']} m"
        )
    shape = (
        "loop"
        if report["loop"]
        else f"ends {report['start_to_end_m'] / 1000:.1f} km from the start"
    )
    lines.append(
        f"shape: {shape}; retraced {_pct(report['retraced_share'])}"
        f" ({report['retraced_km']:.1f} km on ground the route covers twice)"
    )
    if "familiar_share" in report:
        lines.append(
            f"familiar: {_pct(report['familiar_share'])} ({report['familiar_km']:.1f} km) on"
            " ground recorded runs passed before"
        )
    if report.get("surface"):
        lines.append(f"surface: {surface_words(report)}")
    else:
        lines.append("surface: not known for this route")
    if report["segments_known"]:
        if report["segments"]:
            lines.append("stored segments passed, in route order:")
            lines += [f"  {segment_line(p)}" for p in report["segments"]]
        else:
            lines.append("stored segments passed: none")
    return lines
