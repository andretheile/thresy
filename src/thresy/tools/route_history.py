"""The athlete's history for ``coach route``, computed in the database and never sent anywhere.

**Known ground** (:func:`known_cells`): recorded positions of runs reduced to grid cells of
:data:`CELL_M` metres in SQL, one row per distinct cell inside a bounding box. The agent shares
the bridge container's memory limit, so the samples of many activities are never loaded into
Python; what comes back is a set of small integer pairs. The grid is the one of
``thresy.analysis.route`` (``_cells``): metres east and north of an origin, divided by the cell
size and floored. A point of a route counts as *familiar* when its cell or one of the eight
around it is known, i.e. when a recorded run passed within roughly one to two cells.

The query reads ``activity_records`` with a filter on ``lat``/``lon``: there is no index on
positions, so it is a (parallel) sequential scan of the samples table. docs/routes.md has the
measurements and the index or cell table that would make it independent of the table's size.

**Usual start** (:func:`usual_start`): the first recorded position of every run of the last
:data:`USUAL_MONTHS` months (one index lookup per run), clustered on a grid of
:data:`START_CELL_M`: the densest neighbourhood of cells wins, and its median position is the
start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from thresy.tools.route import M_PER_DEG, RouteError

CELL_M = 30.0
"""Edge of a known-ground cell in metres."""
BOX_MARGIN_M = 150.0
"""Margin around a route's bounding box for the known-ground query."""
QUERY_TIMEOUT_S = 30
USUAL_MONTHS = 12
START_CELL_M = 150.0
START_RADIUS_M = 300.0


@dataclass(frozen=True)
class Grid:
    """Cells of ``cell_m`` metres east and north of ``origin`` (lat, lon)."""

    origin: tuple[float, float]
    cell_m: float = CELL_M

    @property
    def kx(self) -> float:
        """Cells per degree of longitude."""
        return M_PER_DEG * math.cos(math.radians(self.origin[0])) / self.cell_m

    @property
    def ky(self) -> float:
        """Cells per degree of latitude."""
        return M_PER_DEG / self.cell_m

    def cells(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """``(n, 2)`` integer cell indices (x, y) of positions, as :func:`known_cells` computes
        them in SQL."""
        cx = np.floor((np.asarray(lon, dtype=float) - self.origin[1]) * self.kx)
        cy = np.floor((np.asarray(lat, dtype=float) - self.origin[0]) * self.ky)
        return np.column_stack([cx, cy]).astype(np.int64)

    def centre(self, cx: np.ndarray, cy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(lat, lon) of cell centres."""
        lat = self.origin[0] + (np.asarray(cy) + 0.5) / self.ky
        lon = self.origin[1] + (np.asarray(cx) + 0.5) / self.kx
        return lat, lon


@dataclass
class Known:
    """Known ground inside ``box`` (lat_min, lat_max, lon_min, lon_max)."""

    grid: Grid
    box: tuple[float, float, float, float]
    cells: set[tuple[int, int]]
    excluded_activity: int | None = None

    def familiar(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Per position: whether its cell or a neighbouring one is known."""
        keys = self.grid.cells(lat, lon)
        out = np.zeros(len(keys), dtype=bool)
        if not self.cells:
            return out
        cells = self.cells
        for i, (x, y) in enumerate(keys.tolist()):
            out[i] = any((x + dx, y + dy) in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        return out


def padded_box(
    box: tuple[float, float, float, float], margin_m: float = BOX_MARGIN_M
) -> tuple[float, float, float, float]:
    lat0, lat1, lon0, lon1 = box
    dlat = margin_m / M_PER_DEG
    dlon = margin_m / (M_PER_DEG * max(math.cos(math.radians((lat0 + lat1) / 2)), 1e-6))
    return lat0 - dlat, lat1 + dlat, lon0 - dlon, lon1 + dlon


KNOWN_SQL = """
SELECT DISTINCT
       floor((r.lon - %(lon0)s) * %(kx)s)::int AS cx,
       floor((r.lat - %(lat0)s) * %(ky)s)::int AS cy
FROM activity_records r
JOIN activities a ON a.id = r.activity_id
WHERE a.is_run
  AND r.lat BETWEEN %(lat_lo)s AND %(lat_hi)s
  AND r.lon BETWEEN %(lon_lo)s AND %(lon_hi)s
  AND r.activity_id <> %(exclude)s
"""


def known_cells(
    conn: Any, box: tuple[float, float, float, float], *, exclude_activity: int | None = None
) -> Known:
    """Distinct cells of recorded runs within ``box`` plus :data:`BOX_MARGIN_M` (see the module
    docstring). ``exclude_activity`` leaves one activity out (``coach route export``)."""
    lat_lo, lat_hi, lon_lo, lon_hi = padded_box(box)
    grid = Grid(origin=(lat_lo, lon_lo))
    params = {
        "lat0": grid.origin[0],
        "lon0": grid.origin[1],
        "kx": grid.kx,
        "ky": grid.ky,
        "lat_lo": lat_lo,
        "lat_hi": lat_hi,
        "lon_lo": lon_lo,
        "lon_hi": lon_hi,
        "exclude": exclude_activity if exclude_activity is not None else -1,
    }
    rows = _query(conn, KNOWN_SQL, params)
    cells = {(int(_value(r, "cx", 0)), int(_value(r, "cy", 1))) for r in rows}
    return Known(grid, (lat_lo, lat_hi, lon_lo, lon_hi), cells, exclude_activity)


def _query(conn: Any, sql: str, params: Any) -> list[Any]:
    """Rows of ``sql`` with a statement timeout, so a slow scan fails instead of hanging."""
    import psycopg

    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = '{int(QUERY_TIMEOUT_S)}s'")
            return conn.execute(sql, params).fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise RouteError(
            f"the history query took longer than {QUERY_TIMEOUT_S} s and was stopped"
        ) from exc


def _value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, dict) else row[index]


# Usual start -----------------------------------------------------------------------------------

STARTS_SQL = """
SELECT a.id, p.lat, p.lon
FROM activities a
CROSS JOIN LATERAL (
  SELECT r.lat, r.lon FROM activity_records r
  WHERE r.activity_id = a.id AND r.lat IS NOT NULL AND r.lon IS NOT NULL
  ORDER BY r.ts LIMIT 1
) p
WHERE a.is_run AND a.has_records AND a.local_date >= %(since)s
"""


@dataclass(frozen=True)
class UsualStart:
    lat: float
    lon: float
    near: int
    """Runs that started within :data:`START_RADIUS_M` of it."""
    runs: int
    """Runs of the period with a recorded position."""
    since: date

    def describe(self) -> str:
        return (
            f"{self.near} of {self.runs} runs since {self.since.isoformat()} started within"
            f" {START_RADIUS_M:.0f} m of it"
        )


def usual_start(conn: Any, today: date, *, months: int = USUAL_MONTHS) -> UsualStart:
    since = today - timedelta(days=round(months * 365.25 / 12))
    rows = _query(conn, STARTS_SQL, {"since": since})
    if not rows:
        raise RouteError(
            f"no runs with recorded positions since {since.isoformat()}, so there is no usual"
            " start: give --start as LAT,LON, a place or activity:ID"
        )
    lat = np.array([float(_value(r, "lat", 1)) for r in rows])
    lon = np.array([float(_value(r, "lon", 2)) for r in rows])
    return cluster_start(lat, lon, since)


def cluster_start(lat: np.ndarray, lon: np.ndarray, since: date) -> UsualStart:
    """The median of the starts in the densest 3x3 block of :data:`START_CELL_M` cells."""
    origin = (float(np.median(lat)), float(np.median(lon)))
    grid = Grid(origin=origin, cell_m=START_CELL_M)
    keys = grid.cells(lat, lon)
    counts: dict[tuple[int, int], int] = {}
    for x, y in keys:
        counts[(int(x), int(y))] = counts.get((int(x), int(y)), 0) + 1

    def block(key: tuple[int, int]) -> int:
        return sum(
            counts.get((key[0] + dx, key[1] + dy), 0) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        )

    best = max(counts, key=lambda k: (block(k), counts[k]))
    inside = (np.abs(keys[:, 0] - best[0]) <= 1) & (np.abs(keys[:, 1] - best[1]) <= 1)
    centre = (float(np.median(lat[inside])), float(np.median(lon[inside])))
    dy = (lat - centre[0]) * M_PER_DEG
    dx = (lon - centre[1]) * M_PER_DEG * math.cos(math.radians(centre[0]))
    near = int((np.hypot(dx, dy) <= START_RADIUS_M).sum())
    return UsualStart(centre[0], centre[1], near, len(lat), since)


def activity_point(conn: Any, activity_id: int, *, last: bool = False) -> tuple[float, float]:
    """The first (or last) recorded position of one activity."""
    order = "DESC" if last else "ASC"
    rows = conn.execute(
        "SELECT lat, lon FROM activity_records WHERE activity_id = %s AND lat IS NOT NULL"
        f" AND lon IS NOT NULL ORDER BY ts {order} LIMIT 1",
        (activity_id,),
    ).fetchall()
    if not rows:
        raise RouteError(f"activity {activity_id} has no recorded positions")
    return float(_value(rows[0], "lat", 0)), float(_value(rows[0], "lon", 1))
