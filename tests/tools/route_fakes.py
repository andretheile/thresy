"""Fakes for the coach route tests: a routing service and a tile server. All values are made up.

:class:`FakeORS` answers ``POST …/v2/directions/<profile>/geojson`` like openrouteservice, with
synthetic geometry built from the request:

* a round trip is a closed polygon (``ROUND_TRIP_SIDES`` corners) through the start, whose
  length is the requested length times a factor that depends on the seed
  (:func:`round_trip_factor`), so the planner has to correct it;
* a route through points goes from each point to the next over a corner placed beside the
  straight line (``DETOUR``), which makes a leg about 1.2 times its straight length; legs
  shorter than ``STRAIGHT_BELOW_M`` (a stored segment, say) go straight.

Elevation rises and falls with the northward position; surface and way type shares are fixed.
"""

from __future__ import annotations

import io
import itertools
import json
import math
from typing import Any

import httpx
import numpy as np

from thresy.tools.route import M_PER_DEG

ORIGIN = (10.0, 20.0)  # a made-up position (as tests/analysis/synth.py)
ROUND_TRIP_SIDES = 5
DETOUR = 0.33  # corner offset as a share of the leg's straight length
STRAIGHT_BELOW_M = 1200.0
STEP_M = 25.0


def round_trip_factor(seed: int) -> float:
    """Actual length / requested length for a seed: 1.3, 0.85, 1.12, 0.95, 1.2, ..."""
    return (1.3, 0.85, 1.12, 0.95, 1.2, 0.78)[(seed - 1) % 6]


def offset(point: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
    lat = point[0] + north_m / M_PER_DEG
    lon = point[1] + east_m / (M_PER_DEG * math.cos(math.radians(point[0])))
    return lat, lon


def _xy(points: list[tuple[float, float]], origin: tuple[float, float]) -> np.ndarray:
    lat = np.array([p[0] for p in points])
    lon = np.array([p[1] for p in points])
    x = (lon - origin[1]) * M_PER_DEG * math.cos(math.radians(origin[0]))
    y = (lat - origin[0]) * M_PER_DEG
    return np.column_stack([x, y])


def _densify(xy: np.ndarray, step: float = STEP_M) -> np.ndarray:
    return _densify_marked(xy, step)[0]


def _densify_marked(xy: np.ndarray, step: float = STEP_M) -> tuple[np.ndarray, list[int]]:
    """Points every ``step`` metres, and the index each corner ended up at."""
    out = [xy[0]]
    at = [0]
    for a, b in itertools.pairwise(xy):
        n = max(int(np.hypot(*(b - a)) // step), 1)
        for k in range(1, n + 1):
            out.append(a + (b - a) * k / n)
        at.append(len(out) - 1)
    return np.array(out), at


class FakeORS:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.status = 200
        self.error: dict[str, Any] | str | None = None
        self.fail_after: int | None = None
        """Answer with ``status``/``error`` from this request number on (1-based)."""
        self.fixed_round_trip_m: float | None = None
        """Every round trip this long, whatever length is asked (it never converges)."""
        self.refuse_detours = False
        """No path near any detour point (a point sent with a search radius of 1000 m)."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        failing = self.fail_after is not None and len(self.requests) >= self.fail_after
        if self.status != 200 and (self.fail_after is None or failing):
            payload = {"error": self.error} if self.error is not None else {}
            return httpx.Response(self.status, json=payload)
        if self.refuse_detours and 1000 in (body.get("radiuses") or []):
            error = {"code": 2010, "message": "Could not find routable point"}
            return httpx.Response(404, json={"error": error})
        coords = [(c[1], c[0]) for c in body["coordinates"]]
        trip = (body.get("options") or {}).get("round_trip")
        if trip:
            features = [
                self._round_trip(coords[0], float(trip["length"]), int(trip.get("seed", 1)))
            ]
        else:
            features = [self._through(coords, side=1.0)]
            alt = body.get("alternative_routes")
            if alt:
                for k in range(1, int(alt["target_count"])):
                    features.append(self._through(coords, side=-1.0 if k == 1 else 2.0))
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": features,
                "metadata": {"attribution": "openrouteservice.org | OpenStreetMap contributors"},
            },
        )

    def _feature(self, xy: np.ndarray, origin: tuple[float, float], way_points: list[int]) -> dict:
        lat = origin[0] + xy[:, 1] / M_PER_DEG
        lon = origin[1] + xy[:, 0] / (M_PER_DEG * math.cos(math.radians(origin[0])))
        ele = 100.0 + 0.02 * (xy[:, 1] - xy[:, 1].min())
        length = float(np.hypot(*np.diff(xy, axis=0).T).sum())
        n = len(xy) - 1
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [round(float(o), 7), round(float(a), 7), round(float(e), 1)]
                    for a, o, e in zip(lat, lon, ele, strict=True)
                ],
            },
            "properties": {
                "summary": {"distance": round(length, 1), "duration": length / 1.3},
                "ascent": 12.0,
                "descent": 12.0,
                "way_points": way_points,
                "extras": {
                    "surface": {
                        "values": [[0, n // 2, 3], [n // 2, n, 10]],
                        "summary": [
                            {"value": 3.0, "distance": length * 0.6, "amount": 60.0},
                            {"value": 10.0, "distance": length * 0.35, "amount": 35.0},
                            {"value": 0.0, "distance": length * 0.05, "amount": 5.0},
                        ],
                    },
                    "waytype": {
                        "values": [[0, n, 4]],
                        "summary": [
                            {"value": 4.0, "distance": length * 0.7, "amount": 70.0},
                            {"value": 3.0, "distance": length * 0.3, "amount": 30.0},
                        ],
                    },
                },
            },
        }

    def _round_trip(self, start: tuple[float, float], length: float, seed: int) -> dict:
        actual = self.fixed_round_trip_m or length * round_trip_factor(seed)
        sides = ROUND_TRIP_SIDES
        radius = actual / (2 * sides * math.sin(math.pi / sides))
        turn = seed * 1.1
        centre = np.array([math.cos(turn), math.sin(turn)]) * radius
        angles = turn + math.pi + np.arange(sides + 1) * 2 * math.pi / sides
        corners = centre + radius * np.column_stack([np.cos(angles), np.sin(angles)])
        corners[0] = corners[-1] = (0.0, 0.0)
        xy = _densify(corners)
        return self._feature(xy, start, [0, len(xy) - 1])

    def _through(self, points: list[tuple[float, float]], side: float) -> dict:
        origin = points[0]
        xy = _xy(points, origin)
        corners = [xy[0]]
        asked = [0]  # which corners are the points asked for (the way points)
        for a, b in itertools.pairwise(xy):
            d = b - a
            if np.hypot(*d) >= STRAIGHT_BELOW_M:
                normal = np.array([-d[1], d[0]])
                corners.append((a + b) / 2 + normal * DETOUR * side)
            corners.append(b)
            asked.append(len(corners) - 1)
        dense, at = _densify_marked(np.array(corners))
        return self._feature(dense, origin, [at[i] for i in asked])


def mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def png_tile(shade: float = 0.9) -> bytes:
    """A plain 256x256 PNG."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.image as mpimg

    buf = io.BytesIO()
    mpimg.imsave(buf, np.full((256, 256, 3), shade), format="png")
    return buf.getvalue()


class FakeTiles:
    """A tile server: every tile is the same plain PNG, with caching headers."""

    def __init__(self, status: int = 200, cache_control: str = "max-age=86400") -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.cache_control = cache_control
        self.tile = png_tile()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="blocked")
        if request.headers.get("If-None-Match") == '"tile-1"':
            return httpx.Response(304, headers={"Cache-Control": self.cache_control})
        return httpx.Response(
            200,
            content=self.tile,
            headers={
                "Content-Type": "image/png",
                "Cache-Control": self.cache_control,
                "ETag": '"tile-1"',
            },
        )
