"""How hilly is a course, and how does it climb? Gradients of fixed-length pieces, compared
between courses.

A course is a distance array and a smoothed altitude array along it (any spacing; the distance
never decreases). Everything below works on those two arrays, so other code (the route tool,
for instance) can call it with a course of its own:

* **Pieces** (:func:`cut`): the stretch is cut into pieces of ``bin_m`` metres (default 100)
  from its start; a last piece shorter than half a piece is dropped. The altitude at a piece's
  edge is the altitude where the distance first reaches that edge
  (:func:`thresy.analysis.data.first_reach`), so standing still or a watch pause creates no
  piece and no gradient. Gradient = altitude change over the piece / its length, in %, rounded
  to 0.1 % before it is classed (a piece shown as 5.0 % counts as steep).
* **Ascent and descent** are summed over the pieces. Ups and downs inside one piece cancel, so
  the sum is lower than a watch's own total, which counts every small rise.
* **Bands** (:func:`bands`): steep down (at most -``steep_pct``), gentle down, flat (below
  ``flat_pct`` either way), gentle up, steep up (at least ``steep_pct``); defaults 1 % and 5 %.
* **Classes** (:func:`classes`): ``class_pct`` wide (default 2) and centred on 0, ±2, ±4 …, so
  the class around 0 is the flat band; the outermost classes collect everything beyond
  ``class_limit_pct`` minus half a class (±9 % by default).
* **Climbs** (:func:`climbs`): the altitude at the piece edges is cut at its turning points
  (:func:`thresy.analysis.activity.turning_points`: a climb ends once the altitude has fallen
  :data:`CLIMB_REVERSAL_M` below its top, so a short dip does not split a long climb); pieces
  below the flat limit at either end are trimmed off. A climb counts with at least
  ``min_gain_m`` (default 10 m) and an average gradient of at least the flat limit.
* **Steep stretches** (:func:`stretches`): runs of consecutive steep pieces, up or down.
* **Likeness** (:func:`overlap_pct`): the overlap of two courses' distributions of distance
  over the classes, each as a share of its own length: the sum over the classes of the
  smaller share. 100 % means the same share of each course in every class, 0 % that no class
  holds distance of both.

Altitude comes in smoothed: activities use :func:`thresy.analysis.data.smooth_altitude` (a
centred mean over 15 samples, about 45 m at running pace); a file's elevation is resampled
every :data:`FILE_STEP_M` along the track and smoothed with :func:`smooth_by_distance` over
:data:`FILE_SMOOTH_M`, the window ``coach route`` uses for its ascent.

The words (:func:`course_words`, :func:`comparison_words`) follow the fixed thresholds below,
which ``coach analysis grades --help`` lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from thresy.analysis.activity import turning_points
from thresy.analysis.data import AnalysisError, first_reach

BIN_M = 100.0
FLAT_PCT = 1.0
STEEP_PCT = 5.0
CLASS_PCT = 2.0
CLASS_LIMIT_PCT = 10.0
CLIMB_REVERSAL_M = 5.0
"""A climb ends once the altitude at the piece edges has fallen this far below its top. Half
the 10 m of ``coach analysis activity``: piece edges are already smooth, and the short rollers
this command should tell apart are small."""
CLIMB_MIN_GAIN_M = 10.0
CLIMBS_LISTED = 5
MAX_COURSES = 4
FILE_STEP_M = 10.0
FILE_SMOOTH_M = 70.0
"""Centred mean over this distance for a file's elevation (the same as coach route's report)."""

# Words: thresholds
HILL_LEVELS_M_PER_KM = (5.0, 12.0, 25.0)
HILL_WORDS = ("flat", "gently rolling", "hilly", "very hilly")
"""The words and limits of ``coach route``, applied to the ascent summed over the pieces."""
STEEP_SHARE_LEVELS = (0.25, 0.5)
STEEP_SHARE_WORDS = ("mostly gentle", "partly steep", "mostly steep")
SHORT_M = 500.0
"""A steep stretch shorter than this is a "short steep ramp"; climbs shorter than this are
short climbs."""
LONG_CLIMB_M = 1000.0
CLIMB_MAJORITY = 0.5
"""Climbs of a length class must hold at least this share of all the climbs' gain to name how
a course climbs."""
CLIMB_KIND_WORDS = {
    "long_gentle": "mostly long gentle climbs",
    "long_steep": "mostly long steep climbs",
    "short_gentle": "mostly short gentle climbs",
    "short_steep": "mostly short steep climbs",
    "mixed": "climbs of mixed length",
}
MORE_RATIO = 1.25
MORE_M_PER_KM = 2.0
"""Climbing per km differs when the larger value is at least 1.25 times the smaller and at
least 2 m/km more."""
MORE_STEEP_POINTS = 2.0
"""Steep uphill differs when its share of the distance differs by at least 2 points."""
LIKE_LEVELS_PCT = (50.0, 70.0, 85.0)
LIKE_WORDS = ("different", "partly alike", "alike", "very alike")
RAMPS_NAMED = 5
LONG_STEEP_NAMED = 3

BANDS = ("steep_down", "gentle_down", "flat", "gentle_up", "steep_up")
BAND_WORDS = {
    "steep_down": "steep down",
    "gentle_down": "gentle down",
    "flat": "flat",
    "gentle_up": "gentle up",
    "steep_up": "steep up",
}
KEYS = "ABCD"


# Settings -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """The options of the method; the defaults are the module constants."""

    bin_m: float = BIN_M
    flat_pct: float = FLAT_PCT
    steep_pct: float = STEEP_PCT
    class_pct: float = CLASS_PCT
    class_limit_pct: float = CLASS_LIMIT_PCT
    min_climb_m: float = CLIMB_MIN_GAIN_M
    climbs: int = CLIMBS_LISTED

    def check(self) -> None:
        if not 10 <= self.bin_m <= 5000:
            raise AnalysisError("--bin-m must lie between 10 and 5000 m")
        if not 0 < self.flat_pct < self.steep_pct:
            raise AnalysisError("thresholds must satisfy 0 < --flat-pct < --steep-pct")
        if not 0.1 <= self.class_pct <= 20:
            raise AnalysisError("--class-pct must lie between 0.1 and 20")
        if self.class_limit_pct < self.class_pct:
            raise AnalysisError("--class-limit must be at least --class-pct")
        if self.min_climb_m < 0 or self.climbs < 0:
            raise AnalysisError("--min-climb-m and --climbs cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "bin_m": self.bin_m,
            "flat_pct": self.flat_pct,
            "steep_pct": self.steep_pct,
            "class_pct": self.class_pct,
            "class_limit_pct": self.class_limit_pct,
            "min_climb_m": self.min_climb_m,
            "climbs_listed": self.climbs,
        }


# Pieces ---------------------------------------------------------------------------------------


@dataclass
class Pieces:
    """A stretch cut into pieces: course distance and altitude at every edge."""

    edges_m: np.ndarray
    alt: np.ndarray

    @property
    def length_m(self) -> np.ndarray:
        return np.diff(self.edges_m)

    @property
    def change_m(self) -> np.ndarray:
        return np.diff(self.alt)

    @property
    def grade_pct(self) -> np.ndarray:
        """Gradient per piece in %, rounded to 0.1 % (the value that is classed and shown)."""
        return np.round(self.change_m / self.length_m * 100, 1) + 0.0

    def __len__(self) -> int:
        return len(self.edges_m) - 1


def cut(
    dist: np.ndarray,
    alt: np.ndarray,
    *,
    bin_m: float = BIN_M,
    from_m: float | None = None,
    to_m: float | None = None,
) -> Pieces:
    """Pieces of ``bin_m`` from ``from_m`` to ``to_m`` (default: the whole course).

    ``dist`` must not decrease; samples where either value is NaN are left out. The altitude at
    an edge is taken where the distance first reaches it, so a stop (distance not advancing)
    adds nothing. A last piece shorter than half ``bin_m`` is dropped.
    """
    dist = np.asarray(dist, dtype="float64")
    alt = np.asarray(alt, dtype="float64")
    ok = ~(np.isnan(dist) | np.isnan(alt))
    dist, alt = dist[ok], alt[ok]
    if len(dist) < 2 or dist[-1] - dist[0] <= 0:
        raise AnalysisError("the course has no distance with altitude")
    if np.any(np.diff(dist) < 0):
        raise AnalysisError("the distance must not decrease along the course")
    d0 = float(dist[0]) if from_m is None else max(float(from_m), float(dist[0]))
    d1 = float(dist[-1]) if to_m is None else min(float(to_m), float(dist[-1]))
    if d0 >= dist[-1]:
        raise AnalysisError(
            f"the stretch starts at km {d0 / 1000:g}, but the course ends at km "
            f"{dist[-1] / 1000:.2f}"
        )
    edges = np.arange(d0, d1 + 1e-6, bin_m)
    if d1 - edges[-1] >= bin_m / 2:
        edges = np.append(edges, d1)
    if len(edges) < 2:
        raise AnalysisError(
            f"the stretch km {d0 / 1000:.2f}-{d1 / 1000:.2f} is shorter than half a piece "
            f"({bin_m / 2:g} m)"
        )
    at = np.array([first_reach(dist, alt, e) for e in edges])
    return Pieces(edges, at)


def smooth_by_distance(
    dist: np.ndarray,
    ele: np.ndarray,
    *,
    step_m: float = FILE_STEP_M,
    window_m: float = FILE_SMOOTH_M,
) -> tuple[np.ndarray, np.ndarray]:
    """Elevation of a track given by distance, resampled every ``step_m`` and smoothed with a
    centred mean over ``window_m``.

    Near either end the window narrows so it stays centred: a straight slope stays straight
    to the last point, where padding with the end value would bend it. Gaps (NaN) are
    interpolated. Returns the resampled distance and elevation.
    """
    dist = np.asarray(dist, dtype="float64")
    ele = np.asarray(ele, dtype="float64")
    known = ~(np.isnan(dist) | np.isnan(ele))
    if known.sum() < 2:
        raise AnalysisError("the course has fewer than two points with elevation")
    d, e = dist[known], ele[known]
    start, total = float(dist[0]), float(dist[-1])
    at = np.arange(start, total, step_m)
    if total - at[-1] > 1e-6:
        at = np.r_[at, total]
    values = np.interp(at, d, e)
    half = max(round(window_m / step_m), 1) // 2
    n = len(values)
    idx = np.arange(n)
    h = np.minimum(np.minimum(idx, n - 1 - idx), half)
    cs = np.r_[0.0, np.cumsum(values)]
    return at, (cs[idx + h + 1] - cs[idx - h]) / (2 * h + 1)


# Measures -------------------------------------------------------------------------------------


def band_of(grade_pct: np.ndarray, flat_pct: float, steep_pct: float) -> np.ndarray:
    """Index into :data:`BANDS` for every gradient."""
    g = np.asarray(grade_pct, dtype="float64")
    out = np.full(len(g), 2)
    out[g >= flat_pct] = 3
    out[g >= steep_pct] = 4
    out[g <= -flat_pct] = 1
    out[g <= -steep_pct] = 0
    return out


def bands(p: Pieces, th: Thresholds) -> list[dict[str, Any]]:
    """Distance and share of the stretch in each band."""
    idx = band_of(p.grade_pct, th.flat_pct, th.steep_pct)
    length = p.length_m
    total = float(length.sum())
    km = np.bincount(idx, weights=length, minlength=len(BANDS)) / 1000
    f, s = th.flat_pct, th.steep_pct
    ranges = [(None, -s), (-s, -f), (-f, f), (f, s), (s, None)]
    return [
        {
            "band": name,
            "words": BAND_WORDS[name],
            "from_pct": lo,
            "to_pct": hi,
            "km": round(float(km[k]), 2),
            "share_pct": round(float(km[k]) * 1000 / total * 100, 1),
        }
        for k, (name, (lo, hi)) in enumerate(zip(BANDS, ranges, strict=True))
    ]


def class_centres(class_pct: float, limit_pct: float) -> np.ndarray:
    k = max(round(limit_pct / class_pct), 1)
    return np.arange(-k, k + 1) * class_pct


def class_of(grade_pct: np.ndarray, class_pct: float, limit_pct: float) -> np.ndarray:
    """Index into :func:`class_centres` for every gradient. Symmetric about 0: +1 % and -1 %
    both leave the class around 0 (when it is 2 % wide), like the flat band."""
    k = max(round(limit_pct / class_pct), 1)
    g = np.asarray(grade_pct, dtype="float64")
    n = np.sign(g) * np.floor(np.abs(g) / class_pct + 0.5 + 1e-9)
    return (np.clip(n, -k, k) + k).astype(int)


def _pct_text(value: float) -> str:
    return "0" if value == 0 else f"{value:+g}"


def class_label(lo: float | None, hi: float | None) -> str:
    if lo is None:
        return f"<= {_pct_text(hi)}"  # type: ignore[arg-type]
    if hi is None:
        return f">= {_pct_text(lo)}"
    return f"{_pct_text(lo)} to {_pct_text(hi)}"


def classes(p: Pieces, th: Thresholds) -> list[dict[str, Any]]:
    """Distance and share of the stretch per gradient class."""
    centres = class_centres(th.class_pct, th.class_limit_pct)
    idx = class_of(p.grade_pct, th.class_pct, th.class_limit_pct)
    length = p.length_m
    total = float(length.sum())
    km = np.bincount(idx, weights=length, minlength=len(centres)) / 1000
    half = th.class_pct / 2
    out = []
    for k, c in enumerate(centres):
        lo = None if k == 0 else round(float(c - half), 2)
        hi = None if k == len(centres) - 1 else round(float(c + half), 2)
        out.append(
            {
                "centre_pct": round(float(c), 2),
                "from_pct": lo,
                "to_pct": hi,
                "label": class_label(lo, hi),
                "km": round(float(km[k]), 2),
                "share_pct": round(float(km[k]) * 1000 / total * 100, 1),
            }
        )
    return out


def class_shares(p: Pieces, th: Thresholds) -> np.ndarray:
    """Share of the stretch per class (sums to 1), unrounded."""
    centres = class_centres(th.class_pct, th.class_limit_pct)
    idx = class_of(p.grade_pct, th.class_pct, th.class_limit_pct)
    length = p.length_m
    return np.bincount(idx, weights=length, minlength=len(centres)) / float(length.sum())


def overlap_pct(a: np.ndarray, b: np.ndarray) -> float:
    """The overlap of two distributions over the same classes, 0 to 100 %."""
    return float(np.minimum(np.asarray(a), np.asarray(b)).sum() * 100)


def _km(metres: float) -> float:
    return round(float(metres) / 1000, 2)


def _piece_at(p: Pieces, i: int) -> dict[str, Any]:
    return {
        "grade_pct": float(p.grade_pct[i]),
        "from_km": _km(p.edges_m[i]),
        "to_km": _km(p.edges_m[i + 1]),
    }


def steepest(p: Pieces) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The steepest piece up and down (``None`` when no piece goes that way)."""
    g = p.grade_pct
    up = _piece_at(p, int(np.argmax(g))) if g.max() > 0 else None
    down = _piece_at(p, int(np.argmin(g))) if g.min() < 0 else None
    return up, down


def _span(p: Pieces, i: int, j: int) -> dict[str, Any]:
    """Pieces ``i`` to ``j - 1`` (edges ``i`` and ``j``) as one stretch."""
    g = p.grade_pct[i:j]
    length = float(p.edges_m[j] - p.edges_m[i])
    change = float(p.alt[j] - p.alt[i])
    extreme = float(g.max()) if change >= 0 else float(g.min())
    return {
        "from_km": _km(p.edges_m[i]),
        "to_km": _km(p.edges_m[j]),
        "length_km": _km(length),
        "change_m": round(change, 1),
        "grade_pct": round(change / length * 100, 1),
        "steepest_pct": extreme,
        "pieces": j - i,
    }


def climbs(
    p: Pieces,
    *,
    flat_pct: float = FLAT_PCT,
    min_gain_m: float = CLIMB_MIN_GAIN_M,
    reversal_m: float = CLIMB_REVERSAL_M,
) -> list[dict[str, Any]]:
    """Every climb of the stretch in course order (see the module docstring)."""
    alt = p.alt
    g = p.grade_pct
    out = []
    for s, e in pairwise(turning_points(alt, reversal_m)):
        if alt[e] <= alt[s]:
            continue
        i, j = s, e
        while i < j and g[i] < flat_pct:
            i += 1
        while j > i and g[j - 1] < flat_pct:
            j -= 1
        if j <= i:
            continue
        span = _span(p, i, j)
        if span["change_m"] < min_gain_m or span["grade_pct"] < flat_pct:
            continue
        span["gain_m"] = span.pop("change_m")
        out.append(span)
    return out


def stretches(p: Pieces, steep_pct: float, direction: int) -> list[dict[str, Any]]:
    """Runs of consecutive pieces at least ``steep_pct`` steep, uphill (``direction`` 1) or
    downhill (-1), in course order."""
    mask = p.grade_pct * direction >= steep_pct
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]] + 1
    return [_span(p, int(a), int(b)) for a, b in zip(starts, ends, strict=True)]


def climb_kind(found: list[dict[str, Any]], steep_pct: float) -> str | None:
    """How a course climbs (a key of :data:`CLIMB_KIND_WORDS`), from all its climbs: ``long``
    when climbs of at least :data:`LONG_CLIMB_M` hold at least half of the climbs' gain,
    ``short`` when climbs under :data:`SHORT_M` do, otherwise ``mixed``; ``steep`` or
    ``gentle`` from the average gradient of those climbs. ``None`` without climbs."""
    total = sum(c["gain_m"] for c in found)
    if not found or total <= 0:
        return None
    for kind, part in (
        ("long", [c for c in found if c["length_km"] * 1000 >= LONG_CLIMB_M]),
        ("short", [c for c in found if c["length_km"] * 1000 < SHORT_M]),
    ):
        gain = sum(c["gain_m"] for c in part)
        if part and gain >= CLIMB_MAJORITY * total:
            grade = gain / (sum(c["length_km"] for c in part) * 1000) * 100
            return f"{kind}_{'steep' if grade >= steep_pct else 'gentle'}"
    return "mixed"


# One course -----------------------------------------------------------------------------------


@dataclass
class Course:
    """A course to measure: distance (m) and smoothed altitude (m) along it."""

    dist: np.ndarray
    alt: np.ndarray
    label: str
    kind: str
    """``activity`` or ``file``."""
    source: str
    """Where the altitude comes from: ``watch`` (the recording) or ``file``."""
    ref: dict[str, Any] = field(default_factory=dict)
    """What it is: the activity (id, date, name, how it was chosen) or the file (path, track)."""
    recorded_ascent_m: float | None = None
    """The watch's own ascent for the whole activity."""

    @property
    def length_m(self) -> float:
        return float(self.dist[-1] - self.dist[0]) if len(self.dist) else 0.0


SOURCE_WORDS = {
    "watch": "the watch's recording (smoothed over 15 s)",
    "file": f"the file (smoothed over {FILE_SMOOTH_M:g} m)",
}


def measure(
    course: Course,
    th: Thresholds,
    *,
    from_km: float | None = None,
    to_km: float | None = None,
    key: str = "A",
) -> dict[str, Any]:
    """Everything reported about one course; ``_pieces`` holds the pieces for a plot."""
    notes = []
    end_km = float(course.dist[-1]) / 1000
    if to_km is not None and to_km > end_km + 0.005:
        notes.append(f"{key}: the stretch was cut to the course's end at km {end_km:.2f}")
    p = cut(
        course.dist,
        course.alt,
        bin_m=th.bin_m,
        from_m=None if from_km is None else from_km * 1000,
        to_m=None if to_km is None else to_km * 1000,
    )
    change = p.change_m
    length = float(p.length_m.sum())
    ascent = float(change[change > 0].sum())
    descent = float(-change[change < 0].sum())
    band_rows = bands(p, th)
    by_band = {b["band"]: b for b in band_rows}
    up_km = by_band["gentle_up"]["km"] + by_band["steep_up"]["km"]
    found = climbs(p, flat_pct=th.flat_pct, min_gain_m=th.min_climb_m)
    largest = sorted(found, key=lambda c: -c["gain_m"])[: th.climbs]
    listed = [c for c in found if c in largest]
    up, down = steepest(p)
    steep_up = stretches(p, th.steep_pct, 1)
    result: dict[str, Any] = {
        "key": key,
        "label": course.label,
        "kind": course.kind,
        **course.ref,
        "elevation_source": course.source,
        "course_km": round(end_km, 2),
        "stretch": {
            "from_km": _km(p.edges_m[0]),
            "to_km": _km(p.edges_m[-1]),
            "whole": from_km is None and to_km is None,
        },
        "length_km": _km(length),
        "bin_m": th.bin_m,
        "pieces": len(p),
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "ascent_m_per_km": round(ascent / (length / 1000), 1),
        "recorded_ascent_m": None
        if course.recorded_ascent_m is None
        else round(course.recorded_ascent_m),
        "bands": band_rows,
        "uphill_km": round(up_km, 2),
        "uphill_share_pct": round(up_km * 1000 / length * 100, 1),
        "steepest_up": up,
        "steepest_down": down,
        "climbs": listed,
        "climbs_found": len(found),
        "climb_kind": climb_kind(found, th.steep_pct),
        "steep_up": steep_up,
        "short_ramps": len([x for x in steep_up if x["length_km"] * 1000 < SHORT_M]),
        "steep_down": stretches(p, th.steep_pct, -1),
        "classes": classes(p, th),
        "piece_from_km": [_km(x) for x in p.edges_m[:-1]],
        "piece_grade_pct": [float(x) for x in p.grade_pct],
        "notes": notes,
        "_pieces": p,
        "_shares": class_shares(p, th),
        "_climbs_all": found,
    }
    result["summary"] = course_words(result, th)
    return result


# Words ----------------------------------------------------------------------------------------


def hill_word(per_km: float) -> str:
    return HILL_WORDS[sum(per_km >= limit for limit in HILL_LEVELS_M_PER_KM)]


def steep_share_words(gentle_km: float, steep_km: float, th: Thresholds) -> str:
    """How the uphill km split into gentle and steep, in words with the numbers."""
    gentle = f"{th.flat_pct:g}-{th.steep_pct:g} %"
    steep = f"{th.steep_pct:g} % or more"
    if steep_km <= 0:
        return f"all of it gentle ({gentle})"
    if gentle_km <= 0:
        return f"all of it steep ({steep})"
    share = steep_km / (gentle_km + steep_km)
    word = STEEP_SHARE_WORDS[sum(share >= limit for limit in STEEP_SHARE_LEVELS)]
    return f"{word} ({gentle_km:.1f} km at {gentle}, {steep_km:.1f} km at {steep})"


def short_ramps(r: dict[str, Any]) -> list[dict[str, Any]]:
    """The steep uphill stretches shorter than :data:`SHORT_M`."""
    return [s for s in r["steep_up"] if s["length_km"] * 1000 < SHORT_M]


def steep_words(r: dict[str, Any]) -> str | None:
    """Where the steep uphill is: the short ramps by position, the longer steep stretches
    with km, length and gradient."""
    short = short_ramps(r)
    longer = [s for s in r["steep_up"] if s not in short]
    parts = []
    if short:
        at = ", ".join(f"{s['from_km']:.1f}" for s in short[:RAMPS_NAMED])
        if len(short) > RAMPS_NAMED:
            at += f" and {len(short) - RAMPS_NAMED} more"
        top = max(s["steepest_pct"] for s in short)
        noun = "short steep ramp" if len(short) == 1 else "short steep ramps"
        parts.append(
            f"{len(short)} {noun} (under {SHORT_M / 1000:g} km, at km {at}; pieces up to "
            f"{top:.1f} %)"
        )
    if longer:
        named = sorted(longer, key=lambda s: -s["length_km"])[:LONG_STEEP_NAMED]
        text = ", ".join(
            f"km {s['from_km']:.1f}-{s['to_km']:.1f}, {s['length_km']:.1f} km at "
            f"{s['grade_pct']:.1f} %"
            for s in sorted(named, key=lambda s: s["from_km"])
        )
        if len(longer) > len(named):
            text += f"; and {len(longer) - len(named)} more"
        noun = "longer steep stretch" if len(longer) == 1 else "longer steep stretches"
        parts.append(f"{len(longer)} {noun} ({text})")
    return "; ".join(parts) or None


def climb_words(r: dict[str, Any], th: Thresholds) -> str:
    kind = r["climb_kind"]
    found = r["_climbs_all"]
    if kind is None:
        return f"no climb of {th.min_climb_m:g} m or more"
    longest = max(found, key=lambda c: (c["length_km"], c["gain_m"]))
    return (
        f"{CLIMB_KIND_WORDS[kind]} (longest {longest['length_km']:.1f} km at "
        f"{longest['grade_pct']:.1f} %)"
    )


def course_words(r: dict[str, Any], th: Thresholds) -> str:
    """One line of words from one course's numbers, e.g. ``hilly (14 m up per km, 140 m over
    10.0 km); uphill 3.6 km (36 %), mostly gentle (2.9 km at 1-5 %, 0.7 km at 5 % or more); 3
    short steep ramps (under 0.5 km, at km 1.2, 3.1, 6.0; pieces up to 8.1 %); climbs of mixed
    length (longest 0.8 km at 4.0 %)``."""
    per_km = r["ascent_m_per_km"]
    parts = [
        f"{hill_word(per_km)} ({per_km:.0f} m up per km, {r['ascent_m']} m over "
        f"{r['length_km']:.1f} km)"
    ]
    by_band = {b["band"]: b for b in r["bands"]}
    gentle, steep = by_band["gentle_up"]["km"], by_band["steep_up"]["km"]
    if r["uphill_km"] <= 0:
        parts.append(f"no piece climbs {th.flat_pct:g} % or more")
    else:
        parts.append(
            f"uphill {r['uphill_km']:.1f} km ({r['uphill_share_pct']:.0f} %), "
            + steep_share_words(gentle, steep, th)
        )
    places = steep_words(r)
    if places:
        parts.append(places)
    parts.append(climb_words(r, th))
    steep_down = by_band["steep_down"]["km"]
    if steep_down > 0:
        parts.append(f"steep down {steep_down:.1f} km")
    return "; ".join(parts)


def _clearly_more(x: float, y: float) -> bool:
    return x - y >= MORE_M_PER_KM and x >= MORE_RATIO * y


def like_word(overlap: float) -> str:
    return LIKE_WORDS[sum(overlap >= limit for limit in LIKE_LEVELS_PCT)]


def _has(r: dict[str, Any], min_climb_m: float) -> str:
    kind = r["climb_kind"]
    return f"no climb of {min_climb_m:g} m or more" if kind is None else CLIMB_KIND_WORDS[kind]


def comparison_words(
    a: dict[str, Any], b: dict[str, Any], overlap: float, min_climb_m: float = CLIMB_MIN_GAIN_M
) -> str:
    """One line comparing course ``b`` with course ``a`` (the first), e.g. ``Home against Race:
    about as much climbing per km (9 vs 9 m), less steep uphill (0 vs 9 % of the distance),
    short steep ramps 0 vs 5; Home has mostly long gentle climbs, Race has climbs of mixed
    length; gradient classes 48 % alike (different)``."""
    name_a, name_b = a["label"], b["label"]
    pa, pb = a["ascent_m_per_km"], b["ascent_m_per_km"]
    nums = f"({pb:.0f} vs {pa:.0f} m)"
    if _clearly_more(pb, pa):
        climbing = f"more climbing per km {nums}"
    elif _clearly_more(pa, pb):
        climbing = f"less climbing per km {nums}"
    else:
        climbing = f"about as much climbing per km {nums}"
    sa = next(x["share_pct"] for x in a["bands"] if x["band"] == "steep_up")
    sb = next(x["share_pct"] for x in b["bands"] if x["band"] == "steep_up")
    nums = f"({sb:.0f} vs {sa:.0f} % of the distance)"
    if sb - sa >= MORE_STEEP_POINTS:
        steep = f"more steep uphill {nums}"
    elif sa - sb >= MORE_STEEP_POINTS:
        steep = f"less steep uphill {nums}"
    else:
        steep = f"about as much steep uphill {nums}"
    na, nb = len(short_ramps(a)), len(short_ramps(b))
    if na or nb:
        steep += f", short steep ramps {nb} vs {na}"
    ka, kb = a["climb_kind"], b["climb_kind"]
    if ka == kb:
        how = f"both have {_has(a, min_climb_m)}"
    else:
        how = f"{name_b} has {_has(b, min_climb_m)}, {name_a} has {_has(a, min_climb_m)}"
    return (
        f"{name_b} against {name_a}: {climbing}, {steep}; {how}; gradient classes "
        f"{overlap:.0f} % alike ({like_word(overlap)})"
    )


# Several courses ------------------------------------------------------------------------------


def compare(
    courses: list[Course],
    th: Thresholds | None = None,
    stretches_km: list[tuple[float | None, float | None]] | None = None,
) -> dict[str, Any]:
    """Measure one to :data:`MAX_COURSES` courses and compare every later one with the first."""
    th = th or Thresholds()
    th.check()
    if not 1 <= len(courses) <= MAX_COURSES:
        raise AnalysisError(f"give 1 to {MAX_COURSES} courses, not {len(courses)}")
    spans = stretches_km or [(None, None)] * len(courses)
    results = [
        measure(c, th, from_km=lo, to_km=hi, key=KEYS[k])
        for k, (c, (lo, hi)) in enumerate(zip(courses, spans, strict=True))
    ]
    likeness = []
    for i, ra in enumerate(results):
        for rb in results[i + 1 :]:
            value = overlap_pct(ra["_shares"], rb["_shares"])
            likeness.append(
                {
                    "a": ra["key"],
                    "b": rb["key"],
                    "overlap_pct": round(value, 1),
                    "words": like_word(value),
                }
            )
    first = results[0]
    with_first = {x["b"]: x["overlap_pct"] for x in likeness if x["a"] == first["key"]}
    comparison = [
        comparison_words(first, r, with_first[r["key"]], th.min_climb_m) for r in results[1:]
    ]
    notes = [n for r in results for n in r["notes"]]
    sources = {r["elevation_source"] for r in results}
    if len(sources) > 1:
        notes.append(mixed_sources_note(results))
    return {
        "settings": th.as_dict(),
        "courses": results,
        "likeness": likeness,
        "comparison": comparison,
        "notes": notes,
    }


def mixed_sources_note(results: list[dict[str, Any]]) -> str:
    def keys(source: str) -> str:
        return ", ".join(r["key"] for r in results if r["elevation_source"] == source)

    return (
        f"elevation measured differently: {keys('file')} from a file, {keys('watch')} from a "
        "watch. A planned route's elevation comes from a terrain model, which misses short "
        "ramps and can show steps at bridges; a watch's barometer is smooth but drifts with the "
        "weather. Compare the bands and classes; single steep pieces and a few percent of ascent "
        "can differ for that reason alone"
    )


# Loading courses ------------------------------------------------------------------------------

_RANGE = re.compile(r"^(?P<lo>\d+(?:\.\d+)?)?-(?P<hi>\d+(?:\.\d+)?)?$")


def parse_spec(spec: str) -> tuple[str, float | None, float | None]:
    """``REF[@FROM-TO]`` -> ``(REF, FROM, TO)`` in km; either end of the range may be left out
    (``@3-``, ``@-10``)."""
    text = spec.strip()
    if "@" in text:
        head, _, tail = text.rpartition("@")
        match = _RANGE.match(tail.strip())
        if match and (match["lo"] or match["hi"]) and head.strip():
            lo = float(match["lo"]) if match["lo"] else None
            hi = float(match["hi"]) if match["hi"] else None
            if lo is not None and hi is not None and hi <= lo:
                raise AnalysisError(f"{spec!r}: the stretch must end after it starts (km FROM-TO)")
            return head.strip(), lo, hi
    if not text:
        raise AnalysisError("empty course")
    return text, None, None


def is_file_ref(ref: str, workspace: Path | None = None) -> Path | None:
    """The file a course reference names, or ``None`` for an activity reference. Relative
    paths are looked up in the working directory, then in ``workspace``."""
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    if workspace is not None and not path.is_absolute() and (workspace / path).is_file():
        return workspace / path
    if path.suffix.lower() == ".gpx" or "/" in ref:
        raise AnalysisError(f"no such file: {ref}")
    return None


def file_course(path: Path) -> Course:
    """A course from a GPX file: distance from the positions, the file's elevation smoothed by
    distance (:func:`smooth_by_distance`)."""
    # Imported here: thresy.tools uses thresy.analysis, so the analysis package never imports
    # the tools at import time.
    from thresy.tools.route import RouteError, drop_repeats, read_gpx

    try:
        track = drop_repeats(read_gpx(path))
    except RouteError as exc:
        raise AnalysisError(str(exc)) from exc
    if len(track) < 2:
        raise AnalysisError(f"{path.name} has fewer than two distinct points")
    if track.ele is None:
        raise AnalysisError(
            f"{path.name} has no elevation, so its gradients are unknown (export it again with "
            "elevation, or plan it with coach route, which adds elevation)"
        )
    dist = track.cumulative()
    missing = int(np.isnan(track.ele).sum())
    if len(track) - missing < 2:
        raise AnalysisError(f"{path.name} has elevation at fewer than two points")
    d, alt = smooth_by_distance(dist, track.ele)
    ref = {"file": str(path), "track_name": track.name, "points": len(track)}
    if missing:
        ref["points_without_elevation"] = missing
    return Course(d, alt, path.name, "file", "file", ref)


def activity_course(conn: Any, ref: str) -> Course:
    """A course from an activity's per-second records: the watch's distance and its altitude
    smoothed as everywhere in the analysis (:func:`thresy.analysis.data.smooth_altitude`)."""
    from thresy.analysis.data import load_records, resolve_activity, smooth_altitude

    sel = resolve_activity(conn, ref)
    act = sel.activity
    df = load_records(conn, act.id, ["alt", "dist", "speed"])
    if df.empty:
        raise AnalysisError(f"activity {act.id} has no per-second data")
    alt = smooth_altitude(df)
    if np.isnan(alt).all():
        raise AnalysisError(f"activity {act.id} has no altitude in its per-second data")
    dist = df["dist"].to_numpy(dtype="float64")
    ok = ~np.isnan(alt)
    if not ok.any() or dist[ok][-1] <= dist[ok][0]:
        raise AnalysisError(f"activity {act.id} has no distance in its per-second data")
    info = {
        "activity_id": act.id,
        "date": act.local_date,
        "name": act.name,
        "type": act.type,
        "selected": sel.how,
        "activity": act.label,
    }
    label = f"{act.local_date} {act.name or act.type}"
    return Course(dist[ok], alt[ok], label, "activity", "watch", info, act.elev_gain_m)


def load_course(conn: Any, spec: str, workspace: Path | None = None) -> tuple[Course, Any, Any]:
    """A course from ``REF[@FROM-TO]``; ``conn`` may be ``None`` when ``REF`` is a file."""
    ref, lo, hi = parse_spec(spec)
    path = is_file_ref(ref, workspace)
    if path is not None:
        return file_course(path), lo, hi
    if conn is None:
        raise AnalysisError(f"{ref!r} is no file, and activities need the database")
    return activity_course(conn, ref), lo, hi


def plot_name(results: list[dict[str, Any]]) -> str:
    """A deterministic file name for the picture of these courses and stretches."""
    parts = []
    for r in results:
        if r["kind"] == "activity":
            stem = str(r["activity_id"])
        else:
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(r["file"]).stem).strip("-.")[:40]
        if not r["stretch"]["whole"]:
            stem += f"@{r['stretch']['from_km']:g}-{r['stretch']['to_km']:g}"
        parts.append(stem or "course")
    name = "grades-" + "_".join(parts)
    if len(name) > 100:
        import hashlib

        name = name[:90] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
    return name


# Text -----------------------------------------------------------------------------------------

STRETCHES_SHOWN = 6


def _what(r: dict[str, Any]) -> str:
    if r["kind"] == "activity":
        how = "" if r["selected"] == "id" else f" ({r['selected']})"
        text = f"activity {r['activity']}{how}"
    else:
        track = f', track "{r["track_name"]}"' if r.get("track_name") else ""
        text = f"file {r['file']}{track}"
        if r.get("points_without_elevation"):
            text += f" ({r['points_without_elevation']} of {r['points']} points without elevation)"
    s = r["stretch"]
    if s["whole"]:
        where = f"whole course, {r['length_km']:.2f} km"
    else:
        where = f"km {s['from_km']:.2f}-{s['to_km']:.2f} of {r['course_km']:.2f}"
    return (
        f"{text}; {where} in {r['pieces']} pieces; elevation from "
        f"{SOURCE_WORDS[r['elevation_source']]}"
    )


def _piece_text(piece: dict[str, Any] | None) -> str:
    if piece is None:
        return "-"
    return f"{piece['grade_pct']:+.1f} % at km {piece['from_km']:.1f}"


def _stretch_text(s: dict[str, Any]) -> str:
    return (
        f"km {s['from_km']:.1f}-{s['to_km']:.1f} ({s['length_km']:.1f} km, "
        f"{s['grade_pct']:+.1f} %, steepest {s['steepest_pct']:+.1f} %)"
    )


def format_text(result: dict[str, Any]) -> str:
    """The text of ``coach analysis grades``: what each course is, the words, then the
    numbers side by side."""
    from thresy.analysis import fmt

    th = result["settings"]
    rs = result["courses"]
    keys = [r["key"] for r in rs]
    lines = [
        f"Gradients of {th['bin_m']:g} m pieces: flat below {th['flat_pct']:g} %, steep from "
        f"{th['steep_pct']:g} % (definitions: coach analysis grades --help)"
    ]
    lines += [f"{r['key']} = {r['label']}: {_what(r)}" for r in rs]
    lines += [f"note: {n}" for n in result["notes"]]
    lines += ["", "In words (from the numbers below):"]
    lines += [f"{r['key']} {r['label']}: {r['summary']}" for r in rs]
    lines += result["comparison"]

    def row(name: str, values: list[str]) -> dict[str, str]:
        return {"what": name, **dict(zip(keys, values, strict=True))}

    rows = [
        row("length km", [f"{r['length_km']:.2f}" for r in rs]),
        row("ascent m (pieces)", [str(r["ascent_m"]) for r in rs]),
        row("descent m (pieces)", [str(r["descent_m"]) for r in rs]),
    ]
    if any(r["kind"] == "activity" for r in rs):
        rows.append(row("ascent m (watch, all)", [fmt.num(r["recorded_ascent_m"]) for r in rs]))
    rows += [
        row("ascent m per km", [f"{r['ascent_m_per_km']:.1f}" for r in rs]),
        row("uphill km", [f"{r['uphill_km']:.2f}" for r in rs]),
        row("uphill %", [f"{r['uphill_share_pct']:.0f}" for r in rs]),
        row("climbs found", [str(r["climbs_found"]) for r in rs]),
    ]
    lines += ["", fmt.table(rows, [("what", ""), *((k, k) for k in keys)])]
    for r in rs:
        lines.append(
            f"steepest piece {r['key']}: up {_piece_text(r['steepest_up'])}, down "
            f"{_piece_text(r['steepest_down'])}"
        )

    cols = [("band", "band"), ("range", "%")]
    for k in keys:
        cols += [(f"{k}km", f"{k} km"), (f"{k}pct", f"{k} %")]
    band_rows = []
    for i, name in enumerate(BANDS):
        b0 = rs[0]["bands"][i]
        cells = {"band": BAND_WORDS[name], "range": class_label(b0["from_pct"], b0["to_pct"])}
        for r in rs:
            b = r["bands"][i]
            cells[f"{r['key']}km"] = f"{b['km']:.2f}"
            cells[f"{r['key']}pct"] = f"{b['share_pct']:.0f}"
        band_rows.append(cells)
    lines += ["", "Bands (km, and % of each course's length):", fmt.table(band_rows, cols)]

    n_classes = len(rs[0]["classes"])
    used = [i for i in range(n_classes) if any(r["classes"][i]["km"] > 0 for r in rs)]
    first, last = (min(used), max(used)) if used else (0, n_classes - 1)
    class_rows = []
    for i in range(first, last + 1):
        cells = {"class": rs[0]["classes"][i]["label"]}
        for r in rs:
            cl = r["classes"][i]
            cells[f"{r['key']}km"] = f"{cl['km']:.2f}"
            cells[f"{r['key']}pct"] = f"{cl['share_pct']:.0f}"
        class_rows.append(cells)
    hidden = "" if (first, last) == (0, n_classes - 1) else "; empty outer classes left out"
    lines += [
        "",
        f"Gradient classes, {th['class_pct']:g} % wide (km, and % of each course's length"
        f"{hidden}):",
        fmt.table(class_rows, [("class", "class %"), *cols[2:]]),
    ]

    lines += [
        "",
        f"Climbs (at least {th['min_climb_m']:g} m gain; the {th['climbs_listed']} largest of "
        "each course, in course order):",
    ]
    climb_rows = [
        {
            "c": r["key"],
            "km": f"{c['from_km']:.1f}-{c['to_km']:.1f}",
            "len": f"{c['length_km']:.1f}",
            "gain": f"+{c['gain_m']:.0f}",
            "avg": f"{c['grade_pct']:.1f}",
            "max": f"{c['steepest_pct']:.1f}",
        }
        for r in rs
        for c in r["climbs"]
    ]
    climb_cols = [
        ("c", ""),
        ("km", "km"),
        ("len", "km long"),
        ("gain", "gain m"),
        ("avg", "avg %"),
        ("max", "steepest %"),
    ]
    lines.append(fmt.table(climb_rows, climb_cols))
    lines += ["", f"Steep stretches (consecutive pieces at {th['steep_pct']:g} % or more):"]
    for r in rs:
        for word, found in (("up", r["steep_up"]), ("down", r["steep_down"])):
            longest = sorted(found, key=lambda s: -s["length_km"])[:STRETCHES_SHOWN]
            shown = sorted(longest, key=lambda s: s["from_km"])
            text = "; ".join(_stretch_text(s) for s in shown) or "none"
            if len(found) > len(shown):
                text += f"; and {len(found) - len(shown)} shorter (all in --json)"
            lines.append(f"{r['key']} {word}: {text}")
    if result["likeness"]:
        pairs = ", ".join(
            f"{x['a']}-{x['b']} {x['overlap_pct']:.0f} % ({x['words']})" for x in result["likeness"]
        )
        lines += ["", f"Likeness of the gradient classes (overlap; 100 % = the same mix): {pairs}"]
    return "\n".join(lines)
