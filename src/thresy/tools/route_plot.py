"""The picture of a route: a map on top, the elevation profile below, sized for a phone.

The map is drawn in Web Mercator. On it: the athlete's known ground (the grid cells of
``thresy.tools.route_history``) as faint squares, the route with a kilometre mark at every
kilometre (every 2 or 5 km on long routes) and arrows for the direction, start and finish,
stored segments highlighted and named, a scale bar and the credit lines. The profile shows the
same segments as shaded spans. Without elevation the profile panel says so instead of guessing.

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
MAX_TILES = 12
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

    def _store(self, base: Path, data_path: Path, meta_path: Path, content: bytes | None,
               response: httpx.Response, old: dict[str, Any]) -> None:  # fmt: skip
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


def _extent(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """The route's box, padded, widened to an aspect between 0.8 and 1.4 (width / height)."""
    x0, x1, y0, y1 = float(x.min()), float(x.max()), float(y.min()), float(y.max())
    w, h = max(x1 - x0, 200.0), max(y1 - y0, 200.0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = w * 1.16, h * 1.16
    if w / h < 0.8:
        w = h * 0.8
    elif w / h > 1.4:
        h = w / 1.4
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


def _mark_step_km(total_km: float) -> int:
    return 1 if total_km <= 16 else 2 if total_km <= 40 else 5


def _nice_length(metres: float) -> float:
    for step in (100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000):
        if step >= metres:
            return float(step)
    return 50000.0


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
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    from thresy.analysis import plots

    plt = plots._plt()
    lab = plots.labels(lang)
    res = report["_res"]
    x, y = mercator(res.lat, res.lon)
    extent = _extent(x, y)
    aspect = (extent[3] - extent[2]) / (extent[1] - extent[0])
    width = 8.0
    map_h = width * aspect
    fig = plt.figure(figsize=(width, map_h + 2.9))
    grid = fig.add_gridspec(2, 1, height_ratios=[map_h, 2.3], hspace=0.12)
    ax = fig.add_subplot(grid[0])
    ax_p = fig.add_subplot(grid[1])
    info: dict[str, Any] = {"tiles": "off"}

    # background: tiles, then known ground
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
    if known is not None and known.cells:
        _known_layer(ax, known, extent)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color(plots.AXIS)

    # the route, its segments, marks
    ax.plot(x, y, color="white", linewidth=6.0, solid_capstyle="round", zorder=3)
    ax.plot(x, y, color=plots.SERIES[0], linewidth=3.2, solid_capstyle="round", zorder=4)
    cum_km = res.cum / 1000
    for p in report["segments"]:
        sel = (cum_km >= p["from_km"]) & (cum_km <= p["to_km"])
        if sel.sum() < 2:
            continue
        ax.plot(x[sel], y[sel], color="white", linewidth=7.5, solid_capstyle="round", zorder=5)
        ax.plot(
            x[sel], y[sel], color=plots.SERIES[1], linewidth=4.2, solid_capstyle="round", zorder=6
        )
        mid = np.flatnonzero(sel)[sel.sum() // 2]
        ax.annotate(
            p["name"],
            (x[mid], y[mid]),
            xytext=(10, 10),
            textcoords="offset points",
            fontsize=9,
            fontweight="bold",
            color=plots.INK,
            zorder=9,
            bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": plots.SERIES[1], "lw": 1.2},
        )
    total_km = float(cum_km[-1])
    step = _mark_step_km(total_km)
    for k in range(step, int(total_km) + 1, step):
        if total_km - k < 0.15 * step:
            continue
        i = int(np.searchsorted(cum_km, k))
        ax.plot(
            x[i],
            y[i],
            "o",
            markersize=13,
            color="white",
            markeredgecolor=plots.INK_2,
            markeredgewidth=1.2,
            zorder=7,
        )
        ax.text(
            x[i],
            y[i],
            str(k),
            ha="center",
            va="center",
            fontsize=7.5,
            fontweight="bold",
            color=plots.INK,
            zorder=8,
        )
    for k in np.arange(step / 2, total_km, step):
        i = int(np.searchsorted(cum_km, k))
        j = min(i + 3, len(x) - 1)
        if j <= i:
            continue
        ax.annotate(
            "",
            xy=(x[j], y[j]),
            xytext=(x[i], y[i]),
            arrowprops={
                "arrowstyle": "-|>",
                "color": plots.RAMP[-2],
                "lw": 0,
                "mutation_scale": 16,
            },
            zorder=7,
        )
    loop = report["loop"]
    ax.plot(
        x[0],
        y[0],
        "o",
        markersize=12,
        color=plots.INK,
        markeredgecolor="white",
        markeredgewidth=2,
        zorder=9,
    )
    ax.annotate(
        lab["route_start_finish"] if loop else lab["route_start"],
        (x[0], y[0]),
        xytext=(-10, -14),
        textcoords="offset points",
        ha="right",
        fontsize=9,
        fontweight="bold",
        color=plots.INK,
        zorder=9,
        bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.85},
    )
    if not loop:
        ax.plot(
            x[-1],
            y[-1],
            "s",
            markersize=11,
            color="white",
            markeredgecolor=plots.INK,
            markeredgewidth=2.2,
            zorder=9,
        )
        ax.annotate(
            lab["route_finish"],
            (x[-1], y[-1]),
            xytext=(10, -14),
            textcoords="offset points",
            fontsize=9,
            fontweight="bold",
            color=plots.INK,
            zorder=9,
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.85},
        )
    _scale_bar(ax, extent, float(np.mean(res.lat)))
    lines = list(credits or [])
    if info.get("tiles") == "drawn" and tiles is not None:
        lines.insert(0, f"{lab['route_map']}: {tiles.attribution}")
    if lines:
        ax.text(
            0.995,
            0.005,
            "\n".join(lines),
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7,
            color=plots.INK_2,
            zorder=10,
            bbox={"boxstyle": "square,pad=0.3", "fc": "white", "ec": "none", "alpha": 0.85},
        )
    handles = [Line2D([0], [0], color=plots.SERIES[0], lw=3.2, label=lab["route_route"])]
    if report["segments"]:
        handles.append(Line2D([0], [0], color=plots.SERIES[1], lw=4.2, label=lab["route_segment"]))
    if known is not None and known.cells:
        handles.append(Patch(color=plots.SERIES[2], alpha=0.45, label=lab["route_known"]))
    if len(handles) >= 2:
        legend = ax.legend(handles=handles, loc="upper left", frameon=True, fontsize=8.5)
        legend.get_frame().set_alpha(0.9)
        legend.get_frame().set_edgecolor("none")
        legend.set_zorder(11)

    _profile(ax_p, report, lab, plots)
    plots._title(fig, title)
    return plots._save(fig, name, out_dir), info


def _known_layer(ax: Any, known: Any, extent: tuple[float, float, float, float]) -> None:
    """Known cells as faint squares (an image over the grid, drawn under the route)."""
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
        return
    raster = np.zeros((h, w), dtype=bool)
    for cx, cy in known.cells:
        if cx0 <= cx <= cx1 and cy0 <= cy <= cy1:
            raster[cy - cy0, cx - cx0] = True
    if not raster.any():
        return
    rgba = np.zeros((h, w, 4))
    color = tuple(int(plots.SERIES[2][i : i + 2], 16) / 255 for i in (1, 3, 5))
    rgba[raster] = (*color, 0.45)
    lat_e = g.origin[0] + np.array([cy0, cy1 + 1]) / g.ky
    lon_e = g.origin[1] + np.array([cx0, cx1 + 1]) / g.kx
    mx, my = mercator(lat_e, lon_e)
    ax.imshow(
        rgba, extent=(mx[0], mx[1], my[0], my[1]), origin="lower", interpolation="nearest", zorder=1
    )


def _scale_bar(ax: Any, extent: tuple[float, float, float, float], lat: float) -> None:
    from thresy.analysis import plots

    scale = 1 / math.cos(math.radians(lat))  # mercator metres per ground metre
    width_m = (extent[1] - extent[0]) / scale
    length = _nice_length(width_m / 5)
    x0 = extent[0] + 0.03 * (extent[1] - extent[0])
    y0 = extent[2] + 0.035 * (extent[3] - extent[2])
    x1 = x0 + length * scale
    ax.plot([x0, x1], [y0, y0], color=plots.INK, linewidth=3, solid_capstyle="butt", zorder=10)
    text = f"{length / 1000:g} km" if length >= 1000 else f"{length:.0f} m"
    ax.text(
        (x0 + x1) / 2,
        y0,
        text,
        ha="center",
        va="bottom",
        fontsize=8,
        color=plots.INK,
        zorder=10,
        bbox={"boxstyle": "square,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.8},
    )


def _profile(ax: Any, report: dict[str, Any], lab: dict[str, str], plots: Any) -> None:
    from thresy.tools.route_report import smoothed_elevation

    res = report["_res"]
    km = res.cum / 1000
    ele = smoothed_elevation(res)
    ax.set_xlim(0, float(km[-1]))
    ax.set_xlabel(lab["distance"])
    if ele is None:
        ax.set_yticks([])
        ax.text(
            0.5,
            0.5,
            lab["route_no_elevation"],
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=10,
            color=plots.INK_2,
        )
    else:
        floor = float(np.min(ele)) - max((float(np.max(ele)) - float(np.min(ele))) * 0.15, 5)
        ax.fill_between(km, ele, floor, color=plots.RAMP[0], alpha=0.5, linewidth=0)
        ax.plot(km, ele, color=plots.SERIES[0], linewidth=1.8)
        ax.set_ylim(bottom=floor)
        ax.set_ylabel(lab["altitude"])
    for p in report["segments"]:
        ax.axvspan(p["from_km"], p["to_km"], color=plots.SERIES[1], alpha=0.18, linewidth=0)
        ax.annotate(
            p["name"],
            ((p["from_km"] + p["to_km"]) / 2, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(0, -3),
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=8,
            color=plots.INK,
            clip_on=True,
        )
