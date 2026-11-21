"""Candidate routes for ``coach route plan``, with at most :data:`MAX_REQUESTS` service requests.

Three cases:

* **A loop with only a distance** (no via points, no ``--to``): round trips from the start with
  different seeds. The service treats the length as "a preferred value", so after a first
  sweep the seeds furthest from the target are asked again with the length scaled by
  target / actual, until enough candidates lie within the tolerance
  (``route_report.target_tolerance_m``) or the requests are used up.
* **Points to pass, no distance**: one route through start, via points and end (or back to the
  start). For start and end only, the service's alternative routes give further candidates.
* **Points to pass and a distance**: the route through the points first. If it is shorter than
  the target, each candidate adds one detour point beside one leg (the longest legs first,
  outward for loops), placed by geometry from the missing distance and the detour of the road
  network so far, then corrected by the secant method over up to :data:`DETOUR_STEPS`
  requests. Legs inside a stored segment (``segment:KEY``) never get a detour. A route that is
  already longer than the target cannot be made shorter and is returned as it is.

The planner returns every route it got; :func:`rank` orders them for the athlete's
preference after the caller has measured them, and drops near-duplicates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from thresy.tools.route import Point, RouteError, project, unproject
from thresy.tools.route_provider import (
    ORS_DEFAULT_RADIUS_M,
    RoutedPath,
    RouteQuery,
    RoutingError,
    RoutingProvider,
)
from thresy.tools.route_report import target_tolerance_m

MAX_CANDIDATES = 5
MAX_REQUESTS = 10
"""Hard cap of routing requests per ``coach route plan``."""
ROUND_TRIP_POINTS = 4
DETOUR_RADIUS_M = 1000.0
DETOUR_STEPS = 3
MAX_ALTERNATIVE_STRAIGHT_M = 80_000.0
PREFERENCES = ("familiar", "new", "none")
STOPPING = ("quota", "rate_limit", "auth", "unavailable")


@dataclass(frozen=True)
class Via:
    points: tuple[tuple[float, float], ...]
    """One point, or a stored segment's start and end (run from the first to the second)."""
    label: str
    segment_key: str | None = None


@dataclass
class PlanRequest:
    start: Point
    end: Point | None
    """``None``: back to the start."""
    vias: list[Via] = field(default_factory=list)
    distance_m: float | None = None
    candidates: int = 3
    trails: bool = False
    avoid_steps: bool = False

    @property
    def loop(self) -> bool:
        return self.end is None


@dataclass
class Planned:
    paths: list[RoutedPath]
    requests: int
    method: str
    notes: list[str] = field(default_factory=list)
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    """The points asked for (start, via points, end), for snapping checks."""


class _Budget:
    def __init__(self, provider: RoutingProvider, limit: int) -> None:
        self.provider = provider
        self.limit = limit
        self.used = 0

    @property
    def left(self) -> int:
        return self.limit - self.used

    def route(self, query: RouteQuery) -> list[RoutedPath]:
        if self.left <= 0:
            raise RouteError("request budget used up")
        self.used += 1
        return self.provider.route(query)


def _length(path: RoutedPath) -> float:
    return path.track.length_m()


def plan(
    provider: RoutingProvider, req: PlanRequest, *, max_requests: int = MAX_REQUESTS
) -> Planned:
    if not 1 <= req.candidates <= MAX_CANDIDATES:
        raise RouteError(f"--candidates takes 1 to {MAX_CANDIDATES}")
    budget = _Budget(provider, max_requests)
    if req.loop and not req.vias:
        if not req.distance_m:
            raise RouteError("a loop needs --distance KM; or give --via points or --to")
        return _round_trips(budget, req)
    return _through_points(budget, req)


def _query(req: PlanRequest, **kw: Any) -> RouteQuery:
    return RouteQuery(trails=req.trails, avoid_steps=req.avoid_steps, **kw)


# Round trips -----------------------------------------------------------------------------------


def _round_trips(budget: _Budget, req: PlanRequest) -> Planned:
    target = float(req.distance_m or 0)
    tol = target_tolerance_m(target)
    start = (req.start.lat, req.start.lon)
    pool: list[RoutedPath] = []
    notes: list[str] = []
    tried: dict[int, list[tuple[float, float]]] = {}  # seed -> [(length asked, length got)]

    def ask(seed: int, length: float) -> bool:
        """One round trip; False when the service stopped answering (what came is kept)."""
        query = _query(
            req,
            points=(start,),
            round_trip_m=length,
            seed=seed,
            round_trip_points=ROUND_TRIP_POINTS,
        )
        try:
            path = budget.route(query)[0]
        except RoutingError as exc:
            if exc.kind == "no_route":  # this seed found nothing: the next one may
                tried.setdefault(seed, []).append((length, math.inf))
                return True
            if pool and exc.kind in STOPPING:  # keep what came
                notes.append(f"stopped early: {exc}")
                return False
            raise
        pool.append(path)
        tried.setdefault(seed, []).append((length, _length(path)))
        return True

    first = min(req.candidates + 2, budget.left, 6)
    for seed in range(1, first + 1):
        if not ask(seed, target):
            return Planned(pool, budget.used, "round trip", notes, [start])

    def close_count() -> int:
        return sum(abs(_length(p) - target) <= tol for p in pool)

    while budget.left > 0 and close_count() < req.candidates:
        # the seed whose best attempt is nearest the target without reaching it, at most 3 tries
        open_seeds = []
        for seed, attempts in tried.items():
            best = min(attempts, key=lambda a: abs(a[1] - target))
            if math.isfinite(best[1]) and abs(best[1] - target) > tol and len(attempts) < 3:
                open_seeds.append((abs(best[1] - target), seed, best))
        if not open_seeds:
            break
        _, seed, (asked, got) = min(open_seeds)
        scale = min(max(target / max(got, 1.0), 0.4), 1.8)
        if not ask(seed, asked * scale):
            break
    if not pool:
        raise RoutingError("the routing service found no round trip from this start", "no_route")
    if close_count() == 0:
        best = min(pool, key=lambda p: abs(_length(p) - target))
        notes.append(
            f"no round trip within {tol / 1000:.2f} km of the target after {budget.used}"
            f" requests; the closest is {_length(best) / 1000:.1f} km"
        )
    return Planned(pool, budget.used, "round trip", notes, [start])


# Through points --------------------------------------------------------------------------------


def _waypoints(req: PlanRequest) -> tuple[list[tuple[float, float]], set[int]]:
    """Points in order, and the legs that must stay as they are: leg ``i`` runs from point ``i``
    to point ``i + 1``, and the leg from a segment's start to its end is locked."""
    points = [(req.start.lat, req.start.lon)]
    locked: set[int] = set()
    for via in req.vias:
        if via.segment_key and len(via.points) == 2:
            locked.add(len(points))
        points += list(via.points)
    end = req.end if req.end is not None else req.start
    points.append((end.lat, end.lon))
    return points, locked


def _through_points(budget: _Budget, req: PlanRequest) -> Planned:
    points, locked = _waypoints(req)
    radii = tuple(ORS_DEFAULT_RADIUS_M for _ in points)
    notes: list[str] = []
    alternatives = 0
    simple = not req.vias and not req.distance_m and req.candidates > 1
    if simple and _polyline_m(points) <= MAX_ALTERNATIVE_STRAIGHT_M:
        alternatives = req.candidates - 1
    base = budget.route(
        _query(req, points=tuple(points), radiuses_m=radii, alternatives=alternatives)
    )
    pool = list(base)
    method = "alternatives" if alternatives else "through the points"
    if not req.distance_m:
        if req.candidates > len(pool):
            notes.append(
                "only one route through these points (give --distance for variants with a detour)"
                if not alternatives
                else f"the service found {len(pool)} different route(s)"
            )
        return Planned(pool, budget.used, method, notes, points)
    target = float(req.distance_m)
    tol = target_tolerance_m(target)
    first = base[0]
    d0 = _length(first)
    if d0 >= target - tol:
        if d0 > target + tol:
            notes.append(
                f"the route through these points is already {d0 / 1000:.1f} km, more than the"
                f" {target / 1000:g} km target; a detour cannot make it shorter"
            )
        return Planned(pool, budget.used, method, notes, points)
    detour_paths, detour_notes = _detours(budget, req, points, radii, locked, d0)
    pool += detour_paths
    notes += detour_notes
    return Planned(pool, budget.used, "detours", notes, points)


def _polyline_m(points: list[tuple[float, float]]) -> float:
    lat = np.array([p[0] for p in points])
    lon = np.array([p[1] for p in points])
    xy = project(lat, lon, points[0])
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(points) > 1 else 0.0


def detour_offset_m(leg_m: float, extra_m: float) -> float:
    """How far beside a leg's midpoint a detour point lies so that going through it adds
    ``extra_m`` (straight lines)."""
    if leg_m < 1.0:
        return extra_m / 2
    return math.sqrt(max(((leg_m + extra_m) / 2) ** 2 - (leg_m / 2) ** 2, 0.0))


def _placements(
    points: list[tuple[float, float]], locked: set[int], loop: bool
) -> list[tuple[int, np.ndarray, float, np.ndarray]]:
    """(leg, unit normal, leg length, leg midpoint) in metres around the start, best first."""
    origin = points[0]
    xy = project(np.array([p[0] for p in points]), np.array([p[1] for p in points]), origin)
    unique = xy[:-1] if loop else xy
    centre = unique.mean(axis=0)
    out = []
    for leg in range(len(points) - 1):
        if leg in locked:  # the leg from a segment's start to its end
            continue
        a, b = xy[leg], xy[leg + 1]
        d = b - a
        length = float(np.hypot(*d))
        mid = (a + b) / 2
        if length < 1.0:
            for n in ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)):
                out.append((leg, np.array(n), length, mid, 0.0))
            continue
        left = np.array([-d[1], d[0]]) / length
        outward = float(np.dot(left, mid - centre))
        first, second = (left, -left) if outward >= 0 or not loop else (-left, left)
        out.append((leg, first, length, mid, 0.0))
        out.append((leg, second, length, mid, 1.0))
    # longer legs first; for each leg the outward side before the inward one
    out.sort(key=lambda p: (p[4], -p[2]))
    return [(leg, n, length, mid) for leg, n, length, mid, _ in out]


def _detours(
    budget: _Budget,
    req: PlanRequest,
    points: list[tuple[float, float]],
    radii: tuple[float, ...],
    locked: set[int],
    d0: float,
) -> tuple[list[RoutedPath], list[str]]:
    target = float(req.distance_m or 0)
    tol = target_tolerance_m(target)
    origin = points[0]
    straight = _polyline_m(points)
    factor = min(max(d0 / straight, 1.1), 2.5) if straight > 1 else 1.3
    missing = target - d0
    placements = _placements(points, locked, req.loop)
    notes: list[str] = []
    if not placements:
        notes.append("no leg can take a detour (every leg lies inside a stored segment)")
        return [], notes
    want = min(req.candidates, len(placements))
    pool: list[RoutedPath] = []
    for k, (leg, normal, length, mid) in enumerate(placements[:want]):
        left = budget.left
        share = max(1, min(DETOUR_STEPS, left // max(want - k, 1)))
        if left <= 0:
            notes.append(f"request budget used up after {budget.used} requests")
            break
        extra = missing / factor
        history = [(0.0, d0)]
        for _ in range(share):
            if budget.left <= 0:
                break
            offset = detour_offset_m(length, extra)
            spot = (mid + normal * offset)[None, :]
            lat, lon = unproject(spot, origin)
            detour = (float(lat[0]), float(lon[0]))
            query_points = (*points[: leg + 1], detour, *points[leg + 1 :])
            query_radii = (*radii[: leg + 1], DETOUR_RADIUS_M, *radii[leg + 1 :])
            try:
                path = budget.route(
                    _query(req, points=tuple(query_points), radiuses_m=tuple(query_radii))
                )[0]
            except RoutingError as exc:
                if exc.kind in ("no_route", "point"):
                    break  # no path near this detour point (water, a field): the next one
                notes.append(f"stopped early: {exc}")  # the route through the points stays
                return pool, notes
            pool.append(path)
            got = _length(path)
            history.append((extra, got))
            if abs(got - target) <= tol:
                break
            (e1, g1), (e2, g2) = history[-2], history[-1]
            slope = (g2 - g1) / (e2 - e1) if abs(e2 - e1) > 1e-6 else 0.0
            if slope < 0.3:
                slope = factor
            extra = max(e2 + (target - g2) / slope, 50.0)
    return pool, notes


# Ranking ---------------------------------------------------------------------------------------


def _shape_rank(retraced: float) -> int:
    return 0 if retraced < 0.15 else 1 if retraced < 0.5 else 2


def rank(reports: list[dict[str, Any]], prefer: str, target_m: float | None) -> list[dict]:
    """``reports`` ordered for the athlete: within the target tolerance first, then by the
    preference (``familiar``: most ground run before; ``new``: least; compared in steps of
    10 %, so a few cells more or less do not outweigh the shape), real loops before
    out-and-back shapes, then by the distance to the target. Near-duplicates (85 % of their
    ground shared) are dropped, keeping the better one."""

    def key(r: dict[str, Any]) -> tuple:
        off = abs(r.get("off_target_km") or 0.0)
        ok = r.get("close_to_target", True)
        fam = r.get("familiar_share") or 0.0
        pref = {"familiar": -round(fam, 1), "new": round(fam, 1)}.get(prefer, 0.0)
        return (not ok, pref, _shape_rank(r["retraced_share"]), off)

    ordered = sorted(reports, key=key)
    kept: list[dict[str, Any]] = []
    for r in ordered:
        cells = r["_cells"]
        if any(len(cells & k["_cells"]) / max(len(cells | k["_cells"]), 1) >= 0.85 for k in kept):
            continue
        kept.append(r)
    return kept


def route_cells(
    lat: np.ndarray, lon: np.ndarray, origin: tuple[float, float]
) -> set[tuple[int, int]]:
    """Cells of 30 m a route covers, for telling near-identical routes apart."""
    xy = project(lat, lon, origin)
    keys = np.floor(xy / 30.0).astype(np.int64)
    return {(int(x), int(y)) for x, y in keys}
