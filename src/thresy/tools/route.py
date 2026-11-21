"""``coach route``: running routes as GPX files, planned with a routing service or read from a file.

This module holds what every part of the tool shares: the settings, the error type, a route as
a :class:`Track` (positions with optional elevation), local geometry in metres, and reading and
writing GPX. The other modules:

* :mod:`thresy.tools.route_provider`: the routing interface and openrouteservice behind it;
* :mod:`thresy.tools.route_planner`: turning a request (start, distance, via points) into
  candidate routes with a bounded number of service requests;
* :mod:`thresy.tools.route_history`: the athlete's history as grid cells, and the usual start,
  computed in SQL;
* :mod:`thresy.tools.route_report`: the numbers and words reported for a route;
* :mod:`thresy.tools.route_plot`: the picture (map and elevation profile, optional map tiles);
* :mod:`thresy.tools.route_cli`: the commands.

Nothing here writes to the database or to any account: routes are files under ``out/``.
Contract: docs/ARCHITECTURE.md, "Routes"; user documentation: docs/routes.md.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG

M_PER_DEG = 111320.0
"""Metres per degree of latitude (and of longitude at the equator), as in thresy.analysis."""

HOSTED_ORS_URL = "https://api.heigit.org/openrouteservice"
"""The hosted openrouteservice API (api.openrouteservice.org was retired in November 2026)."""
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
TILES_OFF = frozenset({"none", "off", "false", "no", "0"})
PROVIDERS = ("openrouteservice", "none")

MAX_GPX_BYTES = 20 * 1024 * 1024
MAX_GPX_POINTS = 200_000


class RouteError(RuntimeError):
    """Something the caller can fix or should know; the message says what to do."""


class RouteSettings(BaseSettings):
    """The ``COACH_ROUTE_*`` settings (docs/routes.md). The bridge hands the set ones to the
    agent."""

    model_config = SETTINGS_CONFIG

    route_provider: str = "openrouteservice"
    """``COACH_ROUTE_PROVIDER``: ``openrouteservice`` (alias ``ors``) or ``none`` (planning
    off; ``show`` and ``export`` still work)."""

    route_url: str | None = None
    """``COACH_ROUTE_URL``: base URL of the routing service, the part before ``/v2``. Unset:
    the hosted openrouteservice (``HOSTED_ORS_URL``), which needs a key. A self-hosted
    instance, e.g. ``http://ors:8082/ors``, needs none."""

    route_api_key: SecretStr | None = None
    """``COACH_ROUTE_API_KEY``: the key of a free openrouteservice account, sent in the
    ``Authorization`` header, never in a URL."""

    route_tile_url: str | None = None
    """``COACH_ROUTE_TILE_URL``: map tiles for the pictures, a URL template with ``{z}``,
    ``{x}`` and ``{y}``. Unset: the standard OpenStreetMap tiles. ``none`` switches tiles off."""

    route_tile_attribution: str | None = None
    """``COACH_ROUTE_TILE_ATTRIBUTION``: the credit line the tile server requires, drawn on
    every picture with tiles. Unset: ``© OpenStreetMap contributors``."""

    route_tile_cache_dir: Path | None = None
    """``COACH_ROUTE_TILE_CACHE_DIR``: where fetched tiles are kept. Unset:
    ``$XDG_CACHE_HOME/thresy/tiles`` or ``~/.cache/thresy/tiles``."""

    @property
    def provider(self) -> str:
        name = (self.route_provider or "openrouteservice").strip().lower()
        return "openrouteservice" if name in ("ors", "openrouteservice") else name

    @property
    def base_url(self) -> str:
        return (self.route_url or HOSTED_ORS_URL).strip().rstrip("/")

    @property
    def hosted(self) -> bool:
        return not (self.route_url and self.route_url.strip())

    @property
    def api_key(self) -> str | None:
        value = self.route_api_key.get_secret_value().strip() if self.route_api_key else ""
        return value or None

    @property
    def tile_url(self) -> str | None:
        value = (self.route_tile_url or "").strip()
        if value.lower() in TILES_OFF:
            return None
        return value or OSM_TILE_URL

    @property
    def tile_attribution(self) -> str:
        value = (self.route_tile_attribution or "").strip()
        return value or OSM_ATTRIBUTION

    @property
    def tile_cache_dir(self) -> Path:
        if self.route_tile_cache_dir:
            return Path(self.route_tile_cache_dir).expanduser()
        import os

        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(base) / "thresy" / "tiles"

    def planning_problem(self) -> str | None:
        """Why planning cannot work with these settings (``None``: it can)."""
        if self.provider == "none":
            return (
                "route planning is switched off (COACH_ROUTE_PROVIDER=none); coach route show"
                " and coach route export still work"
            )
        if self.provider not in PROVIDERS:
            return (
                f"unknown COACH_ROUTE_PROVIDER {self.route_provider!r} (known: openrouteservice,"
                " none)"
            )
        if self.hosted and not self.api_key:
            return (
                "no routing service configured: set COACH_ROUTE_API_KEY to a free openrouteservice"
                " key, or COACH_ROUTE_URL to a self-hosted instance (docs/routes.md); coach route"
                " show and coach route export work without it"
            )
        return None


def agent_route_env(settings: RouteSettings | None = None) -> dict[str, str]:
    """The set ``COACH_ROUTE_*`` values, for the bridge to hand to the agent."""
    s = settings if settings is not None else RouteSettings()
    values = {
        "COACH_ROUTE_PROVIDER": s.route_provider
        if "route_provider" in s.model_fields_set
        else None,
        "COACH_ROUTE_URL": s.route_url,
        "COACH_ROUTE_API_KEY": s.api_key,
        "COACH_ROUTE_TILE_URL": s.route_tile_url,
        "COACH_ROUTE_TILE_ATTRIBUTION": s.route_tile_attribution,
        "COACH_ROUTE_TILE_CACHE_DIR": str(s.route_tile_cache_dir)
        if s.route_tile_cache_dir
        else None,
    }
    return {k: v.strip() for k, v in values.items() if v and v.strip()}


# Points ----------------------------------------------------------------------------------------

_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """``"10.5, 20.25"`` -> ``(10.5, 20.25)`` (latitude first); ``None`` if it is no pair."""
    match = _COORDS.match(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RouteError(f"coordinates out of range: {text!r} (latitude first, then longitude)")
    return lat, lon


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float
    label: str
    """How the point was chosen, in words (``usual start``, ``activity 12 start``, a place)."""

    def coords(self, digits: int = 5) -> str:
        return f"{self.lat:.{digits}f}, {self.lon:.{digits}f}"


# Geometry -------------------------------------------------------------------------------------


def project(lat: np.ndarray, lon: np.ndarray, origin: tuple[float, float]) -> np.ndarray:
    """Positions in metres east and north of ``origin`` (equirectangular, fine for a run)."""
    x = (np.asarray(lon, dtype=float) - origin[1]) * M_PER_DEG * math.cos(math.radians(origin[0]))
    y = (np.asarray(lat, dtype=float) - origin[0]) * M_PER_DEG
    return np.column_stack([x, y])


def unproject(xy: np.ndarray, origin: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    lat = origin[0] + xy[:, 1] / M_PER_DEG
    lon = origin[1] + xy[:, 0] / (M_PER_DEG * math.cos(math.radians(origin[0])))
    return lat, lon


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance between two (lat, lon) points in metres (equirectangular)."""
    xy = project(np.array([a[0], b[0]]), np.array([a[1], b[1]]), a)
    return float(np.hypot(*(xy[1] - xy[0])))


@dataclass
class Track:
    """A route: positions in order, elevation in metres where known."""

    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray | None = None
    """NaN where unknown; ``None`` when the route has no elevation at all."""
    name: str | None = None

    def __post_init__(self) -> None:
        self.lat = np.asarray(self.lat, dtype=float)
        self.lon = np.asarray(self.lon, dtype=float)
        if self.ele is not None:
            ele = np.asarray(self.ele, dtype=float)
            self.ele = None if np.isnan(ele).all() else ele

    def __len__(self) -> int:
        return len(self.lat)

    @property
    def origin(self) -> tuple[float, float]:
        return float(self.lat[0]), float(self.lon[0])

    @property
    def start(self) -> tuple[float, float]:
        return float(self.lat[0]), float(self.lon[0])

    @property
    def end(self) -> tuple[float, float]:
        return float(self.lat[-1]), float(self.lon[-1])

    def xy(self, origin: tuple[float, float] | None = None) -> np.ndarray:
        return project(self.lat, self.lon, origin or self.origin)

    def cumulative(self) -> np.ndarray:
        """Path length in metres at every point."""
        if len(self) < 2:
            return np.zeros(len(self))
        step = np.hypot(*np.diff(self.xy(), axis=0).T)
        return np.r_[0.0, np.cumsum(step)]

    def length_m(self) -> float:
        return float(self.cumulative()[-1]) if len(self) > 1 else 0.0

    def bbox(self) -> tuple[float, float, float, float]:
        """``(lat_min, lat_max, lon_min, lon_max)``."""
        return (
            float(self.lat.min()),
            float(self.lat.max()),
            float(self.lon.min()),
            float(self.lon.max()),
        )

    def subset(self, index: np.ndarray) -> Track:
        ele = None if self.ele is None else self.ele[index]
        return Track(self.lat[index], self.lon[index], ele, self.name)


def drop_repeats(track: Track) -> Track:
    """Without consecutive points at the same position."""
    if len(track) < 2:
        return track
    step = np.hypot(*np.diff(track.xy(), axis=0).T)
    keep = np.r_[True, step > 0.01]
    return track.subset(np.flatnonzero(keep))


@dataclass
class Resampled:
    """A track at even steps along its length (for the measures, not for the files)."""

    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray | None
    cum: np.ndarray
    """Distance along the route in metres at each point."""
    xy: np.ndarray
    """Metres east and north of ``origin``."""
    origin: tuple[float, float]


def resample(track: Track, step_m: float = 10.0) -> Resampled:
    """Points every ``step_m`` metres along the track (and its last point), elevation
    interpolated over gaps."""
    track = drop_repeats(track)
    if len(track) < 2:
        raise RouteError("the route has fewer than two distinct points")
    origin = track.origin
    xy = track.xy(origin)
    cum = track.cumulative()
    total = float(cum[-1])
    at = np.arange(0.0, total, step_m)
    at = np.r_[at, total] if total - at[-1] > 1e-6 else at
    x = np.interp(at, cum, xy[:, 0])
    y = np.interp(at, cum, xy[:, 1])
    pts = np.column_stack([x, y])
    lat, lon = unproject(pts, origin)
    ele = None
    if track.ele is not None:
        known = ~np.isnan(track.ele)
        if known.sum() >= 2:
            ele = np.interp(at, cum[known], track.ele[known])
        elif known.sum() == 1:
            ele = np.full(len(at), float(track.ele[known][0]))
    return Resampled(lat=lat, lon=lon, ele=ele, cum=at, xy=pts, origin=origin)


def simplify(track: Track, tolerance_m: float) -> np.ndarray:
    """Indices of the points kept by Ramer-Douglas-Peucker with ``tolerance_m`` (in metres,
    horizontal only). First and last point are always kept."""
    n = len(track)
    if n <= 2:
        return np.arange(n)
    xy = track.xy()
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = xy[b] - xy[a]
        length = float(np.hypot(*seg))
        rel = xy[a + 1 : b] - xy[a]
        if length < 1e-9:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / length
        k = int(np.argmax(dist))
        if dist[k] > tolerance_m:
            mid = a + 1 + k
            keep[mid] = True
            stack.append((a, mid))
            stack.append((mid, b))
    return np.flatnonzero(keep)


def simplify_to(track: Track, *, tolerance_m: float = 3.0, max_points: int = 2500) -> Track:
    """The track simplified with ``tolerance_m``, raised step by step until at most
    ``max_points`` remain."""
    tol = tolerance_m
    index = simplify(track, tol)
    while len(index) > max_points and tol < 200:
        tol *= 1.5
        index = simplify(track, tol)
    return track.subset(index)


# GPX ------------------------------------------------------------------------------------------


def read_gpx(path: Path) -> Track:
    """The first track of a GPX file (all its segments joined), or else its first route.

    Elevation is kept where the file has it; timestamps, names of points and extensions are
    ignored."""
    import gpxpy
    import gpxpy.gpx

    path = Path(path)
    if not path.is_file():
        raise RouteError(f"no such file: {path}")
    if path.stat().st_size > MAX_GPX_BYTES:
        raise RouteError(f"{path.name} is larger than {MAX_GPX_BYTES // 2**20} MB")
    try:
        gpx = gpxpy.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (gpxpy.gpx.GPXException, ValueError) as exc:
        raise RouteError(f"{path.name} is not a readable GPX file: {exc}") from exc
    name = None
    points: list[tuple[float, float, float | None]] = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            points += [(p.latitude, p.longitude, p.elevation) for p in seg.points]
        if points:
            name = trk.name
            break
    if not points:
        for rte in gpx.routes:
            points = [(p.latitude, p.longitude, p.elevation) for p in rte.points]
            if points:
                name = rte.name
                break
    if len(points) < 2:
        raise RouteError(f"{path.name} has no track or route with at least two points")
    if len(points) > MAX_GPX_POINTS:
        step = math.ceil(len(points) / MAX_GPX_POINTS)
        points = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    lat = np.array([p[0] for p in points], dtype=float)
    lon = np.array([p[1] for p in points], dtype=float)
    ele = np.array([np.nan if p[2] is None else p[2] for p in points], dtype=float)
    return Track(lat, lon, ele, name or gpx.name)


def gpx_xml(track: Track, name: str) -> str:
    """One GPX 1.1 track named ``name``: positions (6 decimals) and elevation (where known),
    nothing else: no time, no author, no metadata."""
    import gpxpy.gpx

    gpx = gpxpy.gpx.GPX()
    gpx.creator = "Thresy"
    trk = gpxpy.gpx.GPXTrack(name=name)
    seg = gpxpy.gpx.GPXTrackSegment()
    trk.segments.append(seg)
    gpx.tracks.append(trk)
    ele = track.ele
    for i in range(len(track)):
        e = None if ele is None or np.isnan(ele[i]) else round(float(ele[i]), 1)
        seg.points.append(
            gpxpy.gpx.GPXTrackPoint(
                round(float(track.lat[i]), 6), round(float(track.lon[i]), 6), elevation=e
            )
        )
    return gpx.to_xml(version="1.1")


def write_gpx(track: Track, path: Path, name: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gpx_xml(track, name), encoding="utf-8")
    return path


def out_dir() -> Path:
    """``<COACH_WORKSPACE_DIR>/out``: where ``coach send`` accepts files from."""
    from thresy.config import get_settings

    return get_settings().workspace_dir.expanduser() / "out"
