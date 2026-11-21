"""The picture of a route: a map on top, the elevation profile below, sized for a phone.

The map is drawn in Web Mercator. On it: the route, solid where it runs on ground the athlete's
recorded runs passed before (the familiar share of the report) and dashed on new ground, with a
kilometre mark at every kilometre (every 2 or 5 km on long routes) and arrows for the
direction, start and finish, stored segments highlighted and named, a scale bar and the credit
lines. Without map tiles, earlier runs show faintly underneath at their true cell size (the
grid cells of ``thresy.tools.route_history``), so even a dense history reads as a network of
paths; over tiles they are left out, because they would hide the map. Labels are placed where
they stay inside the map and clear of the kilometre marks, the markers and each other; a
segment name without room goes into the legend, which sits between map and profile. The
profile shows the same segments as shaded spans. Without elevation the profile panel says so
instead of guessing.

**Map tiles** (optional, ``COACH_ROUTE_TILE_URL``; ``none`` switches them off) come from the
standard OpenStreetMap tile server by default, within its usage policy
(https://operations.osmfoundation.org/policies/tiles/): an identifying User-Agent without
personal data, at most :data:`MAX_TILES` tiles per picture (the zoom is lowered until the route
fits), only the tiles the picture shows, an on-disk cache that honours ``Cache-Control`` /
``Expires`` (7 days when the server sends neither), conditional requests (``If-None-Match``,
``If-Modified-Since``) for expired tiles, no no-cache headers, and the attribution drawn on the
map. When the server refuses (403, 418, 429) no further tiles are asked for in that call, and
the picture is drawn without them.
"""

from __future__ import annotations

import email.utils
import hashlib
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from thresy import __version__

log = logging.getLogger(__name__)

EARTH_R = 6378137.0
MAX_TILES = 16
MIN_ZOOM, MAX_ZOOM = 8, 17
TILE_PX = 256
TILE_TIMEOUT_S = 10.0
DEFAULT_FRESH_S = 7 * 86400
USER_AGENT = f"Thresy/{__version__} (self-hosted running coach)"
TILE_LIGHTEN = 0.35
"""Share of white blended into the tiles, so the route stands out."""
REFUSED = (401, 403, 418, 429)


def mercator(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat = np.clip(np.asarray(lat, dtype=float), -85.05, 85.05)
    x = EARTH_R * np.radians(np.asarray(lon, dtype=float))
    y = EARTH_R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    return x, y


def inverse_mercator(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon = np.degrees(np.asarray(x, dtype=float) / EARTH_R)
    lat = np.degrees(2 * np.arctan(np.exp(np.asarray(y, dtype=float) / EARTH_R)) - np.pi / 2)
    return lat, lon


def tile_span(z: int) -> float:
    """Width of one tile at zoom ``z`` in mercator metres."""
    return 2 * math.pi * EARTH_R / 2**z


def tile_range(extent: tuple[float, float, float, float], z: int) -> tuple[range, range]:
    """Tile columns and rows covering a mercator extent (x0, x1, y0, y1)."""
    span = tile_span(z)
    half = math.pi * EARTH_R
    x0, x1, y0, y1 = extent
    cols = range(int((x0 + half) // span), int((x1 + half) // span) + 1)
    rows = range(int((half - y1) // span), int((half - y0) // span) + 1)
    return cols, rows


def choose_zoom(
    extent: tuple[float, float, float, float], max_tiles: int = MAX_TILES
) -> int | None:
    """The highest zoom at which at most ``max_tiles`` tiles cover the extent."""
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        cols, rows = tile_range(extent, z)
        if len(cols) * len(rows) <= max_tiles:
            return z
    return None


@dataclass
class TileSource:
    """Map tiles from a URL template, through an on-disk cache (see the module docstring)."""

    url: str
    cache_dir: Path
    client: httpx.Client
    attribution: str
    fetched: int = 0
    revalidated: int = 0
    from_cache: int = 0
    problems: list[str] = field(default_factory=list)
    refused: bool = False

    @property
    def host(self) -> str:
        return httpx.URL(self.url.replace("{", "").replace("}", "")).host or "tile server"

    def _dir(self) -> Path:
        digest = hashlib.sha256(self.url.encode()).hexdigest()[:12]
        return self.cache_dir / digest

    def tile_url(self, z: int, x: int, y: int) -> str:
        return (
            self.url.replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
            .replace("{s}", "a")
        )

    def get(self, z: int, x: int, y: int) -> bytes | None:
        base = self._dir() / str(z) / str(x)
        data_path, meta_path = base / f"{y}.tile", base / f"{y}.json"
        meta: dict[str, Any] = {}
        if data_path.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
            if meta and time.time() < float(meta.get("fresh_until", 0)):
                self.from_cache += 1
                return data_path.read_bytes()
        if self.refused:
            return data_path.read_bytes() if data_path.is_file() else None
        headers = {"User-Agent": USER_AGENT}  # whatever client: the policy wants it named
        if meta.get("etag"):
            headers["If-None-Match"] = str(meta["etag"])
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = str(meta["last_modified"])
        try:
            response = self.client.get(self.tile_url(z, x, y), headers=headers)
        except httpx.HTTPError as exc:
            self.problems.append(f"{self.host} unreachable ({type(exc).__name__})")
            self.refused = True
            return data_path.read_bytes() if data_path.is_file() else None
        if response.status_code == 304 and data_path.is_file():
            self.revalidated += 1
            self._store(base, data_path, meta_path, None, response, meta)
            return data_path.read_bytes()
        if response.status_code != 200:
            self.problems.append(f"{self.host} answered HTTP {response.status_code}")
            if response.status_code in REFUSED:
                self.refused = True
            return data_path.read_bytes() if data_path.is_file() else None
        self._store(base, data_path, meta_path, response.content, response, {})
        self.fetched += 1
        return response.content

    def _store(
        self,
        base: Path,
        data_path: Path,
        meta_path: Path,
        content: bytes | None,
        response: httpx.Response,
        old: dict[str, Any],
    ) -> None:
        """Keep a tile (``content``) and its caching headers; a cache that cannot be written
        is reported, and the tile is still used."""
        try:
            base.mkdir(parents=True, exist_ok=True)
            if content is not None:
                data_path.write_bytes(content)
            self._write_meta(meta_path, response, old)
        except OSError as exc:
            problem = f"tile cache not writable ({exc.strerror or type(exc).__name__})"
            if problem not in self.problems:
                self.problems.append(problem)

    @staticmethod
    def _write_meta(path: Path, response: httpx.Response, old: dict[str, Any]) -> None:
        now = time.time()
        meta = {
            "etag": response.headers.get("ETag") or old.get("etag"),
            "last_modified": response.headers.get("Last-Modified") or old.get("last_modified"),
            "fetched_at": now,
            "fresh_until": now + freshness_s(response.headers),
        }
        path.write_text(json.dumps(meta))


def freshness_s(headers: httpx.Headers) -> float:
    """How long a response may be served from the cache: ``max-age`` of ``Cache-Control``, else
    ``Expires``, else :data:`DEFAULT_FRESH_S`."""
    control = headers.get("Cache-Control") or ""
    match = re.search(r"(?:^|[,\s])max-age\s*=\s*(\d+)", control)
    if match:
        return float(match.group(1))
    if "no-cache" in control or "no-store" in control:
        return 0.0
    expires = headers.get("Expires")
    if expires:
        try:
            when = email.utils.parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return max(when.timestamp() - time.time(), 0.0)
    return float(DEFAULT_FRESH_S)


def make_tile_client() -> httpx.Client:
    """The HTTP client for tiles (tests replace it with a mocked transport)."""
    return httpx.Client(
        timeout=TILE_TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )


def _decode(data: bytes) -> np.ndarray | None:
    import matplotlib.image as mpimg

    fmt = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    try:
        img = mpimg.imread(io.BytesIO(data), format=fmt)
    except Exception:  # a broken tile is left out, not fatal
        return None
    img = np.asarray(img)
    if img.dtype == np.uint8:
        img = img.astype(float) / 255.0
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    if img.shape[2] == 4:
        img = img[:, :, :3] * img[:, :, 3:4] + (1 - img[:, :, 3:4])
    return img[:, :, :3]


def mosaic(
    tiles: TileSource, extent: tuple[float, float, float, float]
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """The tiles covering ``extent`` as one image and its mercator extent."""
    z = choose_zoom(extent)
    if z is None:
        tiles.problems.append("the route is too large for a few map tiles")
        return None
    cols, rows = tile_range(extent, z)
    image = np.ones((len(rows) * TILE_PX, len(cols) * TILE_PX, 3))
    got = 0
    for j, y in enumerate(rows):
        for i, x in enumerate(cols):
            data = tiles.get(z, x % 2**z, y)
            img = _decode(data) if data else None
            if img is None or img.shape[0] != TILE_PX or img.shape[1] != TILE_PX:
                continue
            image[j * TILE_PX : (j + 1) * TILE_PX, i * TILE_PX : (i + 1) * TILE_PX] = img
            got += 1
    if got == 0:
        return None
    span = tile_span(z)
    half = math.pi * EARTH_R
    box = (
        cols.start * span - half,
        cols.stop * span - half,
        half - rows.stop * span,
        half - rows.start * span,
    )
    return image * (1 - TILE_LIGHTEN) + TILE_LIGHTEN, box


# The picture -----------------------------------------------------------------------------------

FIG_W_IN = 6.4
"""Figure width: a phone shows the picture about 6 cm wide, so the text is set large."""
TITLE_IN, LEGEND_IN, PROFILE_IN, BOTTOM_IN = 0.55, 0.72, 1.75, 0.55
PROFILE_LEFT_IN, PROFILE_RIGHT_IN = 0.85, 0.12
FONT = {"label": 10.0, "mark": 8.5, "small": 8.0, "legend": 8.5, "credit": 7.0}
STEADY_M = 100.0
"""Stretches of the route shorter than this are drawn like their surroundings (familiar or
new), so crossing a street run before does not chop the line; the reported share is exact."""
KNOWN_COLOR_ALPHA = 0.32
NEW_DASHES = (0, (1.6, 1.1))
"""Dash pattern of the route on new ground, in multiples of the line width."""
KM_MARK_PT = 15.0
START_MARK_PT = 14.0


def _extent(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """The route's box, padded, widened to an aspect between 0.75 and 1.25 (width / height)."""
    x0, x1, y0, y1 = float(x.min()), float(x.max()), float(y.min()), float(y.max())
    w, h = max(x1 - x0, 300.0), max(y1 - y0, 300.0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = w * 1.2, h * 1.2
    if w / h < 0.75:
        w = h * 0.75
    elif w / h > 1.25:
        h = w / 1.25
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


def map_box(lat: np.ndarray, lon: np.ndarray) -> tuple[float, float, float, float]:
    """``(lat_min, lat_max, lon_min, lon_max)`` the picture of a route shows, so the known
    ground can be fetched for all of it."""
    x, y = mercator(lat, lon)
    x0, x1, y0, y1 = _extent(x, y)
    lat_lo, lon_lo = inverse_mercator(np.array([x0]), np.array([y0]))
    lat_hi, lon_hi = inverse_mercator(np.array([x1]), np.array([y1]))
    return float(lat_lo[0]), float(lat_hi[0]), float(lon_lo[0]), float(lon_hi[0])


def _mark_step_km(total_km: float) -> int:
    return 1 if total_km <= 16 else 2 if total_km <= 40 else 5


def _nice_length(metres: float) -> float:
    for step in (100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000):
        if step >= metres:
            return float(step)
    return 50000.0


def _box(fc: str = "white", ec: str = "none", alpha: float = 0.9) -> dict[str, Any]:
    return {"boxstyle": "round,pad=0.25", "fc": fc, "ec": ec, "alpha": alpha}


def steady(mask: np.ndarray, min_points: int) -> np.ndarray:
    """``mask`` with runs shorter than ``min_points`` turned into their neighbours' value."""
    out = mask.copy()
    n = len(out)
    if n == 0 or min_points <= 1:
        return out
    edges = np.flatnonzero(np.diff(out.astype(np.int8))) + 1
    starts = np.r_[0, edges]
    ends = np.r_[edges, n]
    if len(starts) == 1:
        return out
    for a, b in zip(starts, ends, strict=True):
        if b - a < min_points:
            out[a:b] = not mask[a]
    return out


class _Labels:
    """Places text labels in display space so they stay inside the axes and clear of what is
    already there (km marks, markers, the scale bar, the credits, other labels)."""

    PAD_PX = 3.0

    def __init__(self, ax: Any) -> None:
        self.ax = ax
        self.renderer = ax.figure.canvas.get_renderer()
        self.frame = ax.get_window_extent(self.renderer)
        self.taken: list[Any] = []

    def block_point(self, x: float, y: float, size_pt: float) -> None:
        from matplotlib.transforms import Bbox

        cx, cy = self.ax.transData.transform((x, y))
        r = size_pt / 2 * self.ax.figure.dpi / 72 + 1
        self.taken.append(Bbox.from_extents(cx - r, cy - r, cx + r, cy + r))

    def block_artist(self, artist: Any) -> None:
        self.taken.append(artist.get_window_extent(self.renderer))

    def _extent(self, ann: Any) -> Any:
        from matplotlib.text import Text

        ann.update_positions(self.renderer)
        return Text.get_window_extent(ann, self.renderer).padded(self.PAD_PX)

    def fits(self, box: Any) -> bool:
        f = self.frame
        inside = box.x0 >= f.x0 and box.x1 <= f.x1 and box.y0 >= f.y0 and box.y1 <= f.y1
        return inside and not any(box.overlaps(t) for t in self.taken)

    def place(
        self,
        text: str,
        anchors: list[tuple[float, float]],
        tries: list[tuple[float, float, str, str]],
        **kw: Any,
    ) -> Any:
        """The first anchor and offset at which ``text`` fits, or ``None``."""
        for xy in anchors:
            for dx, dy, ha, va in tries:
                ann = self.ax.annotate(
                    text, xy, xytext=(dx, dy), textcoords="offset points", ha=ha, va=va, **kw
                )
                box = self._extent(ann)
                if self.fits(box):
                    self.taken.append(box)
                    return ann
                ann.remove()
        return None


AROUND = [
    (0, -18, "center", "top"),
    (0, 18, "center", "bottom"),
    (16, 0, "left", "center"),
    (-16, 0, "right", "center"),
    (14, -14, "left", "top"),
    (-14, -14, "right", "top"),
    (14, 14, "left", "bottom"),
    (-14, 14, "right", "bottom"),
]
DIAGONAL = [
    (14, 14, "left", "bottom"),
    (-14, 14, "right", "bottom"),
    (14, -14, "left", "top"),
    (-14, -14, "right", "top"),
    (0, 22, "center", "bottom"),
    (0, -22, "center", "top"),
    (22, 0, "left", "center"),
    (-22, 0, "right", "center"),
]


def plot_route(
    title: str,
    report: dict[str, Any],
    *,
    known: Any | None,
    lang: str,
    name: str,
    out_dir: Path | None = None,
    tiles: TileSource | None = None,
    credits: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write ``<out>/<name>.png``; returns its path and what happened with the tiles."""
    from thresy.analysis import plots

    plt = plots._plt()
    lab = plots.labels(lang)
    res = report["_res"]
    x, y = mercator(res.lat, res.lon)
    extent = _extent(x, y)
    aspect = (extent[3] - extent[2]) / (extent[1] - extent[0])
    map_w = FIG_W_IN - 0.1
    map_h = map_w * aspect
    height = TITLE_IN + map_h + LEGEND_IN + PROFILE_IN + BOTTOM_IN
    fig = plt.figure(figsize=(FIG_W_IN, height))
    map_bottom = BOTTOM_IN + PROFILE_IN + LEGEND_IN
    ax = fig.add_axes((0.05 / FIG_W_IN, map_bottom / height, map_w / FIG_W_IN, map_h / height))
    ax_p = fig.add_axes(
        (
            PROFILE_LEFT_IN / FIG_W_IN,
            BOTTOM_IN / height,
            (FIG_W_IN - PROFILE_LEFT_IN - PROFILE_RIGHT_IN) / FIG_W_IN,
            PROFILE_IN / height,
        )
    )
    info: dict[str, Any] = {"tiles": "off"}
    if tiles is not None:
        picture = mosaic(tiles, extent)
        info = {
            "tiles": "drawn" if picture is not None else "unavailable",
            "fetched": tiles.fetched,
            "from_cache": tiles.from_cache,
            "revalidated": tiles.revalidated,
        }
        if tiles.problems:
            info["problem"] = tiles.problems[0]
        if picture is not None:
            image, box = picture
            ax.imshow(image, extent=box, origin="upper", interpolation="bilinear", zorder=0)
    # Known ground as background only without a map: over tiles it would hide the map, and the
    # route line itself shows which stretches were run before.
    background = info["tiles"] != "drawn" and known is not None and bool(known.cells)
    background = background and _known_layer(ax, known, extent)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color(plots.AXIS)

    familiar = report.get("_familiar")
    split = familiar is not None and len(familiar) == len(x)
    _route_layer(ax, x, y, report, familiar if split else None, plots)
    marks = _marks(ax, x, y, report, plots)
    labels = _Labels(ax)
    for mx, my in marks:
        labels.block_point(mx, my, KM_MARK_PT)
    labels.block_point(x[0], y[0], START_MARK_PT)
    if not report["loop"]:
        labels.block_point(x[-1], y[-1], START_MARK_PT)
    for artist in _scale_bar(ax, extent, float(np.mean(res.lat))):
        labels.block_artist(artist)
    lines = list(credits or [])
    if info.get("tiles") == "drawn" and tiles is not None:
        lines.insert(0, f"{lab['route_map']}: {tiles.attribution}")
    if lines:
        credit = ax.text(
            0.995,
            0.006,
            "\n".join(lines),
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=FONT["credit"],
            color=plots.INK_2,
            zorder=10,
            bbox={"boxstyle": "square,pad=0.3", "fc": "white", "ec": "none", "alpha": 0.85},
        )
        labels.block_artist(credit)
    unlabelled = _endpoint_labels(labels, x, y, report["loop"], lab, plots)
    unnamed = _segment_labels(labels, x, y, report, plots)
    _legend(
        fig,
        report,
        split,
        background,
        unlabelled,
        unnamed,
        lab,
        plots,
        top=(BOTTOM_IN + PROFILE_IN + LEGEND_IN - 0.08) / height,
    )
    _profile(ax_p, report, lab, plots)
    plots._title(fig, title)
    return plots._save(fig, name, out_dir), info


def _route_layer(
    ax: Any,
    x: np.ndarray,
    y: np.ndarray,
    report: dict[str, Any],
    familiar: np.ndarray | None,
    plots: Any,
) -> None:
    """The route (blue, white casing): solid where it runs on ground run before, dashed on new
    ground; stored segments in orange on top."""
    ax.plot(x, y, color="white", linewidth=6.5, solid_capstyle="round", zorder=3)
    if familiar is None:
        ax.plot(x, y, color=plots.SERIES[0], linewidth=3.4, solid_capstyle="round", zorder=4)
    else:
        mask = steady(np.asarray(familiar, dtype=bool), round(STEADY_M / _step(report)))
        edges = np.flatnonzero(np.diff(mask.astype(np.int8))) + 1
        for a, b in zip(np.r_[0, edges], np.r_[edges, len(x)], strict=True):
            stop = min(b + 1, len(x))  # join the next stretch
            if mask[a]:
                ax.plot(
                    x[a:stop],
                    y[a:stop],
                    color=plots.SERIES[0],
                    linewidth=3.4,
                    solid_capstyle="round",
                    zorder=4,
                )
            else:
                ax.plot(
                    x[a:stop],
                    y[a:stop],
                    color=plots.SERIES[0],
                    linewidth=3.4,
                    linestyle=NEW_DASHES,
                    dash_capstyle="butt",
                    zorder=4,
                )
    cum_km = report["_res"].cum / 1000
    for p in report["segments"]:
        sel = (cum_km >= p["from_km"]) & (cum_km <= p["to_km"])
        if sel.sum() < 2:
            continue
        ax.plot(x[sel], y[sel], color="white", linewidth=8.5, solid_capstyle="round", zorder=5)
        ax.plot(
            x[sel], y[sel], color=plots.SERIES[1], linewidth=5.0, solid_capstyle="round", zorder=6
        )


def _step(report: dict[str, Any]) -> float:
    cum = report["_res"].cum
    return float(cum[1] - cum[0]) if len(cum) > 1 and cum[1] > cum[0] else 10.0


def _marks(
    ax: Any, x: np.ndarray, y: np.ndarray, report: dict[str, Any], plots: Any
) -> list[tuple[float, float]]:
    """Kilometre marks and direction arrows; returns the marks' positions.

    Arrows sit a little to the right of the direction of travel, so the two directions of an
    out-and-back part stay apart; a kilometre mark that would cover an earlier one moves to the
    right as well."""
    cum_km = report["_res"].cum / 1000
    total_km = float(cum_km[-1])
    step = _mark_step_km(total_km)
    x0, x1 = ax.get_xlim()
    per_point = (x1 - x0) / (ax.get_position().width * ax.figure.get_figwidth() * 72)

    def right_of_travel(i: int) -> np.ndarray:
        j0, j1 = max(i - 2, 0), min(i + 2, len(x) - 1)
        d = np.array([x[j1] - x[j0], y[j1] - y[j0]])
        norm = float(np.hypot(*d))
        return np.zeros(2) if norm == 0 else np.array([d[1], -d[0]]) / norm

    drawn: list[np.ndarray] = []
    for k in range(step, int(total_km) + 1, step):
        if total_km - k < 0.4 * step:  # too close to the finish marker
            continue
        i = int(np.searchsorted(cum_km, k))
        spot = np.array([x[i], y[i]])
        if any(np.hypot(*(spot - p)) < 16 * per_point for p in drawn):
            spot = spot + right_of_travel(i) * 18 * per_point
        drawn.append(spot)
        ax.plot(
            *spot,
            "o",
            markersize=KM_MARK_PT,
            color="white",
            markeredgecolor=plots.INK_2,
            markeredgewidth=1.2,
            zorder=7,
        )
        ax.text(
            *spot,
            str(k),
            ha="center",
            va="center",
            fontsize=FONT["mark"],
            fontweight="bold",
            color=plots.INK,
            zorder=8,
        )
    for k in np.arange(step / 2, total_km - 0.2, step):
        i = int(np.searchsorted(cum_km, k))
        j = min(i + 3, len(x) - 1)
        if j <= i:
            continue
        shift = right_of_travel(i) * 7 * per_point
        ax.annotate(
            "",
            xy=(x[j] + shift[0], y[j] + shift[1]),
            xytext=(x[i] + shift[0], y[i] + shift[1]),
            zorder=7,
            arrowprops={
                "arrowstyle": "-|>",
                "color": plots.RAMP[-1],
                "lw": 0,
                "mutation_scale": 16,
            },
        )
    ax.plot(
        x[0],
        y[0],
        "o",
        markersize=START_MARK_PT,
        color=plots.INK,
        markeredgecolor="white",
        markeredgewidth=2.2,
        zorder=9,
    )
    if not report["loop"]:
        ax.plot(
            x[-1],
            y[-1],
            "s",
            markersize=START_MARK_PT - 1,
            color="white",
            markeredgecolor=plots.INK,
            markeredgewidth=2.4,
            zorder=9,
        )
    return [(float(p[0]), float(p[1])) for p in drawn]


def _endpoint_labels(
    labels: _Labels, x: np.ndarray, y: np.ndarray, loop: bool, lab: dict[str, str], plots: Any
) -> list[str]:
    """Start and finish named next to their markers; returns the names that found no room."""
    style = {
        "fontsize": FONT["label"],
        "fontweight": "bold",
        "color": plots.INK,
        "zorder": 9,
        "bbox": _box(),
    }
    missing = []
    first = lab["route_start_finish"] if loop else lab["route_start"]
    if labels.place(first, [(x[0], y[0])], AROUND, **style) is None:
        missing.append("start")
    if not loop and labels.place(lab["route_finish"], [(x[-1], y[-1])], AROUND, **style) is None:
        missing.append("finish")
    return missing


def _segment_labels(
    labels: _Labels, x: np.ndarray, y: np.ndarray, report: dict[str, Any], plots: Any
) -> list[str]:
    """Stored segments named beside them; returns the names that found no room (they go into
    the legend)."""
    cum_km = report["_res"].cum / 1000
    unnamed: list[str] = []
    done: set[str] = set()
    for p in report["segments"]:
        if p["name"] in done:  # passed twice: one name is enough
            continue
        idx = np.flatnonzero((cum_km >= p["from_km"]) & (cum_km <= p["to_km"]))
        if idx.size < 2:
            continue
        anchors = [
            (x[i], y[i])
            for i in idx[[round(f * (idx.size - 1)) for f in (0.5, 0.3, 0.7, 0.15, 0.85)]]
        ]
        ann = labels.place(
            p["name"],
            anchors,
            DIAGONAL,
            fontsize=FONT["label"],
            fontweight="bold",
            color=plots.INK,
            zorder=9,
            bbox=_box(ec=plots.SERIES[1], alpha=0.95),
            arrowprops={"arrowstyle": "-", "color": plots.SERIES[1], "lw": 1.2},
        )
        done.add(p["name"])
        if ann is None:
            unnamed.append(p["name"])
    return unnamed


def _legend(
    fig: Any,
    report: dict[str, Any],
    split: bool,
    background: bool,
    unlabelled: list[str],
    unnamed: list[str],
    lab: dict[str, str],
    plots: Any,
    *,
    top: float,
) -> None:
    """The legend between map and profile, so it never covers the map."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    blue = plots.SERIES[0]
    if split:
        handles = [
            Line2D([0], [0], color=blue, lw=3.4, label=lab["route_familiar"]),
            Line2D([0], [0], color=blue, lw=3.4, linestyle=NEW_DASHES, label=lab["route_new"]),
        ]
    else:
        handles = [Line2D([0], [0], color=blue, lw=3.4, label=lab["route_route"])]
    if report["segments"]:
        text = lab["route_segment"] + (": " + ", ".join(unnamed) if unnamed else "")
        handles.append(Line2D([0], [0], color=plots.SERIES[1], lw=5, label=text))
    if background:
        handles.append(Patch(color=plots.MUTED, alpha=KNOWN_COLOR_ALPHA, label=lab["route_known"]))
    if "start" in unlabelled:
        name = lab["route_start_finish"] if report["loop"] else lab["route_start"]
        handles.append(
            Line2D([0], [0], marker="o", color=plots.INK, lw=0, markersize=9, label=name)
        )
    if "finish" in unlabelled:
        handles.append(
            Line2D(
                [0],
                [0],
                marker="s",
                color="white",
                markeredgecolor=plots.INK,
                lw=0,
                markersize=8,
                label=lab["route_finish"],
            )
        )
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, top),
        ncol=2,
        frameon=False,
        fontsize=FONT["legend"],
        handlelength=2.6,
        columnspacing=1.6,
    )


def _known_layer(ax: Any, known: Any, extent: tuple[float, float, float, float]) -> bool:
    """Earlier runs as faint marks at their true cell size (no widening), under the route: even
    a dense history then reads as a network of paths, never as a filled area."""
    from thresy.analysis import plots

    lat_lo, lon_lo = inverse_mercator(np.array([extent[0]]), np.array([extent[2]]))
    lat_hi, lon_hi = inverse_mercator(np.array([extent[1]]), np.array([extent[3]]))
    g = known.grid
    lo = g.cells(lat_lo, lon_lo)[0]
    hi = g.cells(lat_hi, lon_hi)[0]
    cx0, cy0 = int(lo[0]) - 1, int(lo[1]) - 1
    cx1, cy1 = int(hi[0]) + 1, int(hi[1]) + 1
    w, h = cx1 - cx0 + 1, cy1 - cy0 + 1
    if w <= 0 or h <= 0 or w * h > 4_000_000:
        return False
    raster = np.zeros((h, w), dtype=bool)
    for cx, cy in known.cells:
        if cx0 <= cx <= cx1 and cy0 <= cy <= cy1:
            raster[cy - cy0, cx - cx0] = True
    if not raster.any():
        return False
    rgba = np.zeros((h, w, 4))
    color = tuple(int(plots.MUTED[i : i + 2], 16) / 255 for i in (1, 3, 5))
    rgba[raster] = (*color, KNOWN_COLOR_ALPHA)
    lat_e = g.origin[0] + np.array([cy0, cy1 + 1]) / g.ky
    lon_e = g.origin[1] + np.array([cx0, cx1 + 1]) / g.kx
    mx, my = mercator(lat_e, lon_e)
    ax.imshow(
        rgba, extent=(mx[0], mx[1], my[0], my[1]), origin="lower", interpolation="nearest", zorder=1
    )
    return True


def _scale_bar(ax: Any, extent: tuple[float, float, float, float], lat: float) -> list[Any]:
    """A scale bar in the top right corner; returns the bar and its label (to keep other labels
    off them)."""
    from thresy.analysis import plots

    scale = 1 / math.cos(math.radians(lat))  # mercator metres per ground metre
    width_m = (extent[1] - extent[0]) / scale
    length = _nice_length(width_m / 5)
    x1 = extent[1] - 0.03 * (extent[1] - extent[0])
    y0 = extent[3] - 0.05 * (extent[3] - extent[2])
    x0 = x1 - length * scale
    (bar,) = ax.plot(
        [x0, x1], [y0, y0], color=plots.INK, linewidth=3, solid_capstyle="butt", zorder=10
    )
    text = f"{length / 1000:g} km" if length >= 1000 else f"{length:.0f} m"
    label = ax.annotate(
        text,
        ((x0 + x1) / 2, y0),
        xytext=(0, 4),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=FONT["small"],
        color=plots.INK,
        zorder=10,
        bbox={"boxstyle": "square,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.8},
    )
    return [bar, label]


def _profile(ax: Any, report: dict[str, Any], lab: dict[str, str], plots: Any) -> None:
    from matplotlib.text import Text

    from thresy.tools.route_report import smoothed_elevation

    res = report["_res"]
    km = res.cum / 1000
    ele = smoothed_elevation(res)
    ax.set_xlim(0, float(km[-1]))
    ax.set_xlabel(lab["distance"], fontsize=FONT["label"])
    ax.tick_params(labelsize=FONT["small"] + 0.5)
    if ele is None:
        ax.set_yticks([])
        ax.text(
            0.5,
            0.5,
            lab["route_no_elevation"],
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=FONT["label"],
            color=plots.INK_2,
        )
    else:
        lo, hi = float(np.min(ele)), float(np.max(ele))
        pad = max((hi - lo) * 0.15, 5.0)
        floor, top = lo - pad, hi + pad + max((hi - lo) * 0.25, 10.0)  # room for segment names
        ax.fill_between(km, ele, floor, color=plots.RAMP[0], alpha=0.5, linewidth=0)
        ax.plot(km, ele, color=plots.SERIES[0], linewidth=2.0)
        ax.set_ylim(floor, top)
        ax.set_ylabel(lab["altitude"], fontsize=FONT["label"])
    # Segment names at the top, moved sideways to stay inside the axes and down a row when they
    # would cover each other.
    renderer = ax.figure.canvas.get_renderer()
    frame = ax.get_window_extent(renderer)
    per_px = 72 / ax.figure.dpi
    placed: list[Any] = []
    for p in report["segments"]:
        ax.axvspan(p["from_km"], p["to_km"], color=plots.SERIES[1], alpha=0.2, linewidth=0)
        centre = ((p["from_km"] + p["to_km"]) / 2, 1.0)
        for row in range(4):
            dy = -4 - row * 13
            ann = ax.annotate(
                p["name"],
                centre,
                xycoords=("data", "axes fraction"),
                xytext=(0, dy),
                textcoords="offset points",
                ha="center",
                va="top",
                fontsize=FONT["small"],
                fontweight="bold",
                color=plots.INK,
            )
            ann.update_positions(renderer)
            box = Text.get_window_extent(ann, renderer)
            shift = max(frame.x0 + 2 - box.x0, 0) - max(box.x1 - (frame.x1 - 2), 0)
            if shift:
                ann.xyann = (shift * per_px, dy)
                ann.update_positions(renderer)
                box = Text.get_window_extent(ann, renderer)
            if not any(box.padded(2).overlaps(b) for b in placed) or row == 3:
                placed.append(box)
                break
            ann.remove()
