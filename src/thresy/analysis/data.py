"""Selecting activities and loading their per-second data as pandas frames.

Record frames returned by :func:`load_records` use short column names:

========  ==================================================================================
``ts``    sample time, UTC (``datetime64[ns, UTC]``)
``t``     seconds since the first record (wall clock, pauses included)
``lat``   ``lon`` degrees; ``alt`` metres; ``dist`` cumulative metres (never decreasing)
``speed`` metres per second; ``hr`` bpm; ``cad`` steps per minute; ``power`` watts
``resp``  breaths per minute; ``temp`` degrees Celsius
``dt``    seconds since the previous sample (0 for the first)
``paused`` the gap before this sample is longer than :data:`PAUSE_GAP_S` (the watch was paused)
``timer`` cumulative timer time: like ``t`` but without paused gaps
``still`` the athlete stands still at this sample (``speed`` < :data:`STILL_SPEED_MPS`)
``still_s`` seconds of standing that start at this sample: the time to the next sample when
          ``still`` and the timer kept running
``stop_s`` seconds stopped that start at this sample: ``still_s`` plus a paused gap that
          follows it
========  ==================================================================================

Times between two samples are differences of ``timer`` (timer time) or ``t`` (wall clock).
Stopped time between samples ``i`` and ``j`` is ``stop_s[i:j].sum()``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.rows import dict_row, tuple_row

Conn = psycopg.Connection[Any]

PAUSE_GAP_S = 10.0
"""A gap between two records longer than this means the watch timer was paused."""
STILL_SPEED_MPS = 0.5
"""Below this speed the athlete counts as standing still."""
MOVING_SPEED_MPS = 1.0
"""Samples at or above this speed count as running or walking for averages such as cadence."""

RECORD_COLUMNS = {
    "ts": "ts",
    "elapsed_s": "t",
    "lat": "lat",
    "lon": "lon",
    "alt_m": "alt",
    "dist_m": "dist",
    "speed_mps": "speed",
    "hr": "hr",
    "cadence_spm": "cad",
    "power_w": "power",
    "resp_rate": "resp",
    "temp_c": "temp",
}
NUMERIC = ("t", "lat", "lon", "alt", "dist", "speed", "hr", "cad", "power", "resp", "temp")


class AnalysisError(RuntimeError):
    """A problem the user can fix (unknown activity, no data, bad option)."""


# Activities -----------------------------------------------------------------------------------

_ACTIVITY_SQL = """
SELECT id, start_utc, start_local, local_date, type, is_run, name, distance_m, duration_s,
       moving_s, avg_hr, max_hr, elev_gain_m, elev_loss_m, avg_cadence_spm, avg_power_w,
       has_records
FROM activities
"""


@dataclass(frozen=True)
class Activity:
    id: int
    start_utc: datetime | None
    start_local: datetime | None
    local_date: date
    type: str
    is_run: bool
    name: str | None
    distance_m: float | None
    duration_s: float | None
    moving_s: float | None
    avg_hr: float | None
    max_hr: float | None
    elev_gain_m: float | None
    elev_loss_m: float | None
    avg_cadence_spm: float | None
    avg_power_w: float | None
    has_records: bool

    @property
    def utc_offset(self) -> timedelta:
        """Local time minus UTC at the start (from Garmin's own local start time)."""
        if self.start_local is None or self.start_utc is None:
            return timedelta(0)
        return self.start_local - self.start_utc.astimezone(UTC).replace(tzinfo=None)

    def local(self, ts: datetime | pd.Timestamp) -> datetime:
        """A UTC timestamp as naive local time of this activity."""
        value = pd.Timestamp(ts)
        if value.tzinfo is None:
            value = value.tz_localize(UTC)
        return (value.tz_convert(UTC).tz_localize(None) + self.utc_offset).to_pydatetime()

    @property
    def label(self) -> str:
        km = f"{self.distance_m / 1000:.1f} km" if self.distance_m else "? km"
        when = self.start_local.strftime("%Y-%m-%d %H:%M") if self.start_local else self.local_date
        return f"{when} {self.name or self.type} ({km}, id {self.id})"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["distance_km"] = round(self.distance_m / 1000, 3) if self.distance_m else None
        return out


def _activity(row: dict[str, Any]) -> Activity:
    return Activity(**row)


def get_activity(conn: Conn, activity_id: int) -> Activity | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ACTIVITY_SQL + " WHERE id = %s", (activity_id,))
        row = cur.fetchone()
    return None if row is None else _activity(row)


def find_activities(
    conn: Conn,
    *,
    since: date | None = None,
    until: date | None = None,
    runs_only: bool = True,
    with_records: bool = False,
    types: Sequence[str] | None = None,
    name: str | None = None,
    limit: int | None = None,
    newest_first: bool = True,
) -> list[Activity]:
    """Activities matching the filters (``name`` is a case-insensitive substring)."""
    where, params = ["true"], []
    if since:
        where.append("local_date >= %s")
        params.append(since)
    if until:
        where.append("local_date <= %s")
        params.append(until)
    if runs_only:
        where.append("is_run")
    if with_records:
        where.append("has_records")
    if types:
        where.append("type = ANY(%s)")
        params.append(list(types))
    if name:
        where.append("name ILIKE %s")
        params.append(f"%{name}%")
    order = "DESC" if newest_first else "ASC"
    sql = _ACTIVITY_SQL + f" WHERE {' AND '.join(where)} ORDER BY start_utc {order} NULLS LAST, id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [_activity(r) for r in cur.fetchall()]


@dataclass(frozen=True)
class Selected:
    activity: Activity
    how: str
    """How it was chosen, e.g. ``"id"``, ``"last run"``, ``"longest run on 2030-01-02"``."""
    alternatives: list[Activity] = field(default_factory=list)
    """Other activities that matched a date selector."""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisError(f"not a date (YYYY-MM-DD): {value!r}") from exc


def resolve_activity(
    conn: Conn,
    ref: str | int | None = None,
    *,
    last: bool = False,
    on_date: date | str | None = None,
    runs_only: bool = True,
    need_records: bool = True,
) -> Selected:
    """Choose one activity.

    ``ref`` is an activity id, ``"last"`` or a date ``YYYY-MM-DD``; ``last`` and ``on_date`` are
    the option forms. Without any selector the most recent run is used. A date picks the longest
    matching activity and lists the others as ``alternatives``. With ``need_records`` only
    activities with per-second data qualify (an explicit id without data is an error).
    """
    if isinstance(ref, str) and ref.strip().lower() == "last":
        ref, last = None, True
    if isinstance(ref, str) and len(ref) == 10 and ref[4] == "-":
        if on_date is not None:
            raise AnalysisError("give either an activity or --date, not both")
        ref, on_date = None, ref
    if sum(x is not None for x in (ref, on_date)) + bool(last) > 1:
        raise AnalysisError("give only one of: an activity id, --last, --date")
    if ref is not None:
        try:
            activity_id = int(ref)
        except (TypeError, ValueError) as exc:
            raise AnalysisError(f"not an activity id, date or 'last': {ref!r}") from exc
        activity = get_activity(conn, activity_id)
        if activity is None:
            raise AnalysisError(f"no activity with id {activity_id}")
        if need_records and not activity.has_records:
            raise AnalysisError(f"activity {activity_id} has no per-second data (no FIT file)")
        return Selected(activity, "id")
    kind = "run" if runs_only else "activity"
    if on_date is not None:
        day = parse_date(on_date) if isinstance(on_date, str) else on_date
        found = find_activities(
            conn, since=day, until=day, runs_only=runs_only, with_records=need_records
        )
        if not found:
            extra = " with per-second data" if need_records else ""
            raise AnalysisError(f"no {kind}{extra} on {day.isoformat()}")
        found.sort(key=lambda a: a.distance_m or 0, reverse=True)
        how = f"longest {kind} on {day.isoformat()}" if len(found) > 1 else f"{kind} on {day}"
        return Selected(found[0], how, found[1:])
    found = find_activities(conn, runs_only=runs_only, with_records=need_records, limit=1)
    if not found:
        raise AnalysisError(f"no {kind} with per-second data" if need_records else f"no {kind}")
    return Selected(found[0], f"last {kind}")


# Records and laps -----------------------------------------------------------------------------


def load_records(
    conn: Conn, activity_id: int, columns: Sequence[str] | None = None
) -> pd.DataFrame:
    """Per-second records of one activity, prepared as described in the module docstring.

    ``columns`` restricts the loaded fields to these short names (``ts`` and ``t`` are always
    included). Returns an empty frame when there are no records.
    """
    wanted = {"ts", "t", *(columns or RECORD_COLUMNS.values())}
    db_cols = [db for db, short in RECORD_COLUMNS.items() if short in wanted]
    sql = f"SELECT {', '.join(db_cols)} FROM activity_records WHERE activity_id = %s ORDER BY ts"
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(sql, (activity_id,))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=[RECORD_COLUMNS[c] for c in db_cols])
    return prepare(df)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the derived columns (``dt``, ``paused``, ``timer``, ``still``) and clean ``dist``
    and ``speed``. Works on any frame with at least ``t`` (and optionally ``ts``)."""
    df = df.copy()
    for col in NUMERIC:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    if "ts" in df and len(df):
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    if df.empty:
        for col in ("dt", "timer", "still_s", "stop_s"):
            df[col] = pd.Series(dtype="float64")
        df["paused"] = pd.Series(dtype=bool)
        df["still"] = pd.Series(dtype=bool)
        return df
    df = df.sort_values("t", kind="stable").reset_index(drop=True)
    dt = df["t"].diff().fillna(0.0).clip(lower=0.0)
    paused = dt > PAUSE_GAP_S
    df["dt"] = dt
    df["paused"] = paused
    df["timer"] = dt.where(~paused, 0.0).cumsum()
    if "speed" in df and "dist" in df and df["dist"].isna().all() and df["speed"].notna().any():
        df["dist"] = (df["speed"].fillna(0.0) * df["dt"].where(~paused, 0.0)).cumsum()
    if "dist" in df:
        df["dist"] = df["dist"].ffill().fillna(0.0).cummax()
    if "speed" in df and "dist" in df:
        derived = (df["dist"].diff() / dt.replace(0.0, np.nan)).where(~paused)
        df["speed"] = df["speed"].fillna(derived)
    if "speed" in df:
        df["still"] = df["speed"].fillna(0.0) < STILL_SPEED_MPS
    else:
        df["still"] = False
    dt_next = dt.shift(-1, fill_value=0.0)
    paused_next = paused.shift(-1, fill_value=False)
    df["still_s"] = dt_next.where(df["still"] & ~paused_next, 0.0)
    df["stop_s"] = df["still_s"] + dt_next.where(paused_next, 0.0)
    return df


def load_laps(conn: Conn, activity_id: int, first_ts: pd.Timestamp | None = None) -> pd.DataFrame:
    """Laps of one activity, with ``t_start``/``t_end`` in seconds since ``first_ts`` (the first
    record's time) when given."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM activity_laps WHERE activity_id = %s ORDER BY lap_no", (activity_id,)
        )
        rows = cur.fetchall()
    laps = pd.DataFrame(rows)
    if laps.empty:
        return laps
    for col in laps.columns:
        if col not in ("start_utc", "intensity", "trigger"):
            laps[col] = pd.to_numeric(laps[col], errors="coerce")
    laps["start_utc"] = pd.to_datetime(laps["start_utc"], utc=True)
    if first_ts is not None:
        laps["t_start"] = (laps["start_utc"] - pd.Timestamp(first_ts)).dt.total_seconds()
        nxt = laps["t_start"].shift(-1)
        own = laps["t_start"] + laps["elapsed_s"].fillna(laps["duration_s"])
        laps["t_end"] = nxt.where(nxt.notna(), own)
    return laps


# Helpers on record frames ---------------------------------------------------------------------


def window(df: pd.DataFrame, t0: float, t1: float) -> pd.DataFrame:
    """Rows with ``t0 <= t < t1``."""
    return df[(df["t"] >= t0) & (df["t"] < t1)]


def interp(df: pd.DataFrame, col: str, t: float) -> float:
    """Linear interpolation of ``col`` at time ``t`` (NaN outside the data)."""
    valid = df[["t", col]].dropna()
    if valid.empty or t < valid["t"].iloc[0] or t > valid["t"].iloc[-1]:
        return float("nan")
    return float(np.interp(t, valid["t"].to_numpy(), valid[col].to_numpy()))


def first_reach(xs: np.ndarray, ys: np.ndarray, x: float) -> float:
    """``y`` at the first point where the non-decreasing ``xs`` reaches ``x`` (interpolated).

    Unlike :func:`numpy.interp` this is well defined on plateaus (standing still), where it
    returns the moment the plateau was reached.
    """
    n = len(xs)
    if n == 0 or x > xs[-1]:
        return float("nan")
    i = int(np.searchsorted(xs, x, side="left"))
    if i == 0:
        return float(ys[0])
    x0, x1 = xs[i - 1], xs[i]
    if x1 <= x0:
        return float(ys[i])
    return float(ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0))


def mean_or_none(values: pd.Series | np.ndarray, digits: int | None = 1) -> float | None:
    arr = np.asarray(values, dtype="float64")
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return None
    value = float(arr.mean())
    return round(value, digits) if digits is not None else value


def max_or_none(values: pd.Series | np.ndarray) -> float | None:
    arr = np.asarray(values, dtype="float64")
    arr = arr[~np.isnan(arr)]
    return float(arr.max()) if arr.size else None


def min_or_none(values: pd.Series | np.ndarray) -> float | None:
    arr = np.asarray(values, dtype="float64")
    arr = arr[~np.isnan(arr)]
    return float(arr.min()) if arr.size else None


def pace_s_per_km(time_s: float | None, dist_m: float | None) -> float | None:
    if not time_s or not dist_m or dist_m <= 0:
        return None
    return time_s / dist_m * 1000


def smooth_altitude(df: pd.DataFrame, window_s: int = 15) -> np.ndarray:
    """Altitude smoothed with a centred rolling mean over ``window_s`` samples (gaps
    interpolated). Returns NaNs when the activity has no altitude."""
    if "alt" not in df or df["alt"].notna().sum() < 2:
        return np.full(len(df), np.nan)
    alt = df["alt"].interpolate(limit_direction="both")
    return alt.rolling(window_s, center=True, min_periods=1).mean().to_numpy()


ASCENT_THRESHOLD_M = 1.0
"""Dead band for ascent and descent: a change counts once the (smoothed) altitude has moved
this far from the last counted point."""


def ascent_profile(
    alt: np.ndarray, threshold_m: float = ASCENT_THRESHOLD_M
) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative ascent and descent per sample of an altitude series, with a dead band of
    ``threshold_m``. NaN altitudes contribute nothing. The ascent between samples ``i`` and
    ``j`` is ``up[j] - up[i]``."""
    n = len(alt)
    up = np.zeros(n)
    down = np.zeros(n)
    ref = np.nan
    total_up = total_down = 0.0
    for i in range(n):
        value = alt[i]
        if not np.isnan(value):
            if np.isnan(ref):
                ref = value
            delta = value - ref
            if delta >= threshold_m:
                total_up += delta
                ref = value
            elif delta <= -threshold_m:
                total_down -= delta
                ref = value
        up[i] = total_up
        down[i] = total_down
    return up, down


def ascent_descent(alt: np.ndarray, threshold_m: float = ASCENT_THRESHOLD_M) -> tuple[float, float]:
    """Total ascent and descent of an altitude series (see :func:`ascent_profile`)."""
    if len(alt) == 0:
        return 0.0, 0.0
    up, down = ascent_profile(np.asarray(alt, dtype="float64"), threshold_m)
    return float(up[-1]), float(down[-1])
