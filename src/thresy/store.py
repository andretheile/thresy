"""The write API for Garmin data, events and sync state.

Every function takes an open connection (see :mod:`thresy.db`) as its first argument and
does its work inside ``conn.transaction()``: on an autocommit connection each call is atomic
and durable when it returns; inside your own ``with conn.transaction():`` block it becomes a
savepoint of your transaction. All upserts are idempotent: calling them again with the same
input leaves the database unchanged.

Raw Garmin payloads are stored as ``jsonb`` next to the derived columns, so the columns can be
recomputed later. Payloads that are ``None``, empty (``{}``) or look like an error
(``{"error": ...}``) are ignored, and a missing value never overwrites a stored one.

Wake date: Garmin keys sleep and HRV by the calendar day on which the athlete woke up.
:func:`upsert_wellness` takes that day as ``wake_date`` and refuses payloads whose own
``calendarDate`` says otherwise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from thresy.fit import LAP_COLUMNS, RECORD_COLUMNS, Lap, Record

log = logging.getLogger(__name__)

Conn = psycopg.Connection[Any]

# Garmin activityType.typeKey values that count as runs.
RUN_TYPES = frozenset(
    {
        "running",
        "trail_running",
        "treadmill_running",
        "track_running",
        "indoor_running",
        "ultra_run",
        "virtual_run",
        "street_running",
    }
)

# Garmin sleepLevels[].activityLevel -> stage. Established empirically: per night, the summed
# durations of each level equal deepSleepSeconds (0), lightSleepSeconds (1), remSleepSeconds (2)
# and awakeSleepSeconds (3) of the same payload.
SLEEP_STAGES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}

# Garmin personal record typeId -> label (as used by the old bot).
PR_LABELS = {
    1: "1k",
    2: "1mi",
    3: "5k",
    4: "10k",
    5: "half_marathon",
    6: "marathon",
    7: "longest_run_m",
    8: "longest_ride",
    12: "most_steps_day",
    13: "most_steps_week",
    14: "most_steps_month",
}


# Helpers --------------------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def to_jsonb(value: Any) -> Jsonb:
    """Wrap ``value`` for a ``jsonb`` parameter; dates and datetimes become ISO strings."""
    return Jsonb(value, dumps=_dumps)


def is_ok_payload(payload: Any) -> bool:
    """True for a non-empty dict payload that is not an error placeholder like
    ``{"error": "..."}``. An empty dict (Garmin's answer for "no data") is not a payload, so it
    can never overwrite a stored raw payload."""
    return isinstance(payload, Mapping) and bool(payload) and "error" not in payload


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def parse_garmin_time(value: Any, *, utc: bool) -> datetime | None:
    """Parse Garmin time strings (``2024-01-02 15:28:15``, ``2024-01-02T21:13:55.0``) or epoch
    milliseconds. With ``utc=True`` the result is timezone-aware UTC, otherwise naive (local)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(value / 1000, tz=UTC)
        return dt if utc else dt.replace(tzinfo=None)
    dt = datetime.fromisoformat(str(value).strip())
    if utc:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.replace(tzinfo=None)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else round(f)


# Activities -----------------------------------------------------------------------------------

ACTIVITY_COLUMNS = (
    "start_utc",
    "start_local",
    "local_date",
    "type",
    "is_run",
    "name",
    "distance_m",
    "duration_s",
    "moving_s",
    "elapsed_s",
    "avg_hr",
    "max_hr",
    "elev_gain_m",
    "elev_loss_m",
    "avg_cadence_spm",
    "avg_power_w",
    "vo2max",
    "aerobic_te",
    "anaerobic_te",
    "training_load",
    "calories",
    "location",
)


def activity_type_key(payload: Mapping[str, Any]) -> str | None:
    """``activityType.typeKey`` of a Garmin activity summary (or the plain string)."""
    t = payload.get("activityType") or payload.get("activityTypeDTO")
    key = t.get("typeKey") if isinstance(t, Mapping) else t
    return str(key) if key else None


def activity_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map a Garmin activity summary to ``activities`` columns (without raw/source).

    ``payload`` is one item as returned by ``garminconnect``'s ``get_activities`` /
    ``get_activities_by_date``. Raises ``ValueError`` without ``activityId`` or start time.
    """
    raw_id = payload.get("activityId")
    if raw_id in (None, ""):
        raise ValueError("activity payload has no activityId")
    start_utc = parse_garmin_time(payload.get("startTimeGMT"), utc=True) or parse_garmin_time(
        payload.get("beginTimestamp"), utc=True
    )
    start_local = parse_garmin_time(payload.get("startTimeLocal"), utc=False)
    if start_local is not None:
        local_date = start_local.date()
    elif start_utc is not None:
        local_date = start_utc.date()
    else:
        raise ValueError(f"activity {raw_id} has no start time")
    type_key = activity_type_key(payload) or "unknown"
    duration = payload.get("duration")
    if duration is None:
        duration = payload.get("elapsedDuration")
    return {
        "id": int(raw_id),
        "start_utc": start_utc,
        "start_local": start_local,
        "local_date": local_date,
        "type": type_key,
        "is_run": type_key in RUN_TYPES,
        "name": payload.get("activityName"),
        "distance_m": _num(payload.get("distance")),
        "duration_s": _num(duration),
        "moving_s": _num(payload.get("movingDuration")),
        "elapsed_s": _num(payload.get("elapsedDuration")),
        "avg_hr": _num(payload.get("averageHR")),
        "max_hr": _num(payload.get("maxHR")),
        "elev_gain_m": _num(payload.get("elevationGain")),
        "elev_loss_m": _num(payload.get("elevationLoss")),
        "avg_cadence_spm": _num(payload.get("averageRunningCadenceInStepsPerMinute")),
        "avg_power_w": _num(payload.get("avgPower")),
        "vo2max": _num(payload.get("vO2MaxValue")),
        "aerobic_te": _num(payload.get("aerobicTrainingEffect")),
        "anaerobic_te": _num(payload.get("anaerobicTrainingEffect")),
        "training_load": _num(payload.get("activityTrainingLoad")),
        "calories": _num(payload.get("calories")),
        "location": payload.get("locationName"),
    }


_ACTIVITY_UPSERT = f"""
INSERT INTO activities (id, {", ".join(ACTIVITY_COLUMNS)}, raw, source)
VALUES (%(id)s, {", ".join(f"%({c})s" for c in ACTIVITY_COLUMNS)}, %(raw)s, %(source)s)
ON CONFLICT (id) DO UPDATE SET
  {", ".join(f"{c} = EXCLUDED.{c}" for c in ACTIVITY_COLUMNS)},
  raw = EXCLUDED.raw,
  source = EXCLUDED.source
WHERE ({", ".join(f"activities.{c}" for c in ACTIVITY_COLUMNS)}, activities.raw, activities.source)
  IS DISTINCT FROM
  ({", ".join(f"EXCLUDED.{c}" for c in ACTIVITY_COLUMNS)}, EXCLUDED.raw, EXCLUDED.source)
RETURNING (xmax = 0) AS inserted
"""


def upsert_activity(conn: Conn, payload: Mapping[str, Any], *, source: str) -> bool:
    """Insert or update one activity from a Garmin activity summary payload.

    ``source`` records where the payload came from (for example ``"garmin"`` or
    ``"legacy"``). The derived columns, ``raw`` and ``source`` are replaced; ``has_records``,
    ``fit_path`` and ``created_at`` are kept. Nothing is written (and ``updated_at`` does not
    move) when the stored row already matches.

    Returns ``True`` if the activity was new (the caller may then raise a ``new_activity``
    event), ``False`` if it already existed.
    """
    row = activity_row(payload)
    params = {**row, "raw": to_jsonb(dict(payload)), "source": source}
    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(_ACTIVITY_UPSERT, params)
        result = cur.fetchone()
    return bool(result and result[0])


def replace_activity_records(
    conn: Conn,
    activity_id: int,
    records: Sequence[Record],
    laps: Sequence[Lap],
    *,
    fit_path: str | None = None,
) -> int:
    """Replace all ``activity_records`` and ``activity_laps`` of one activity.

    Rows are bulk-loaded with COPY. ``activities.has_records`` is set to whether ``records``
    is non-empty, and ``activities.fit_path`` is set when ``fit_path`` is given (a path
    relative to ``COACH_DATA_DIR``). The activity must already exist (``LookupError``
    otherwise). Typical use::

        data = thresy.fit.parse_fit(path)
        replace_activity_records(conn, activity_id, data.records, data.laps, fit_path=rel)

    Returns the number of records written.
    """
    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT 1 FROM activities WHERE id = %s FOR UPDATE", (activity_id,))
        if cur.fetchone() is None:
            raise LookupError(f"activity {activity_id} does not exist")
        cur.execute("DELETE FROM activity_records WHERE activity_id = %s", (activity_id,))
        cur.execute("DELETE FROM activity_laps WHERE activity_id = %s", (activity_id,))
        if records:
            with cur.copy(
                f"COPY activity_records (activity_id, {', '.join(RECORD_COLUMNS)}) FROM STDIN"
            ) as copy:
                for r in records:
                    copy.write_row(
                        (
                            activity_id,
                            r.ts,
                            r.elapsed_s,
                            r.lat,
                            r.lon,
                            r.alt_m,
                            r.dist_m,
                            r.speed_mps,
                            r.hr,
                            r.cadence_spm,
                            r.power_w,
                            r.resp_rate,
                            r.temp_c,
                        )
                    )
        if laps:
            with cur.copy(
                f"COPY activity_laps (activity_id, {', '.join(LAP_COLUMNS)}) FROM STDIN"
            ) as copy:
                for lap in laps:
                    copy.write_row((activity_id, *(getattr(lap, c) for c in LAP_COLUMNS)))
        cur.execute(
            "UPDATE activities SET has_records = %s, fit_path = coalesce(%s, fit_path)"
            " WHERE id = %s",
            (bool(records), fit_path, activity_id),
        )
    return len(records)


# Wellness -------------------------------------------------------------------------------------


def is_real_sleep(sleep: Any) -> bool:
    """True when a Garmin sleep payload contains a recorded night.

    Before the watch has synced, Garmin answers with a ``dailySleepDTO`` that has a
    ``calendarDate`` but no ``sleepTimeSeconds``; that is not real sleep data.
    """
    if not is_ok_payload(sleep):
        return False
    dto = sleep.get("dailySleepDTO") or {}
    seconds = _num(dto.get("sleepTimeSeconds"))
    return seconds is not None and seconds > 0


def _check_calendar_date(kind: str, found: Any, wake_date: date) -> None:
    if found and str(found)[:10] != wake_date.isoformat():
        raise ValueError(
            f"{kind} payload is for {str(found)[:10]}, not for wake date {wake_date.isoformat()}"
        )


def wellness_row(
    wake_date: date | str,
    *,
    sleep: Any = None,
    hrv: Any = None,
    stats: Any = None,
    steps: Any = None,
) -> dict[str, Any]:
    """Map Garmin wellness payloads for one wake date to ``wellness_daily`` columns.

    Only columns with a value are returned (plus ``wake_date``). ``sleep`` is
    ``get_sleep_data(day)``, ``hrv`` is ``get_hrv_data(day)``, ``stats`` is ``get_stats(day)``
    and ``steps`` is the optional ``get_steps_data(day)`` interval list, used for the step count
    only when ``stats`` has none. Raises ``ValueError`` if a payload belongs to another day.
    """
    day = _as_date(wake_date)
    row: dict[str, Any] = {"wake_date": day}

    if is_ok_payload(sleep):
        dto = sleep.get("dailySleepDTO") or {}
        _check_calendar_date("sleep", dto.get("calendarDate"), day)
        if is_real_sleep(sleep):
            scores = dto.get("sleepScores") or {}
            overall = scores.get("overall") if isinstance(scores, Mapping) else None
            row.update(
                sleep_score=_int(overall.get("value")) if isinstance(overall, Mapping) else None,
                sleep_s=_int(dto.get("sleepTimeSeconds")),
                deep_s=_int(dto.get("deepSleepSeconds")),
                light_s=_int(dto.get("lightSleepSeconds")),
                rem_s=_int(dto.get("remSleepSeconds")),
                awake_s=_int(dto.get("awakeSleepSeconds")),
                sleep_start_utc=parse_garmin_time(dto.get("sleepStartTimestampGMT"), utc=True),
                sleep_end_utc=parse_garmin_time(dto.get("sleepEndTimestampGMT"), utc=True),
            )

    if is_ok_payload(hrv):
        summary = hrv.get("hrvSummary") or {}
        _check_calendar_date("hrv", summary.get("calendarDate"), day)
        baseline = summary.get("baseline") or {}
        row.update(
            hrv_night_avg=_num(summary.get("lastNightAvg")),
            hrv_weekly_avg=_num(summary.get("weeklyAvg")),
            hrv_status=summary.get("status"),
            hrv_balanced_low=_num(baseline.get("balancedLow")),
            hrv_balanced_high=_num(baseline.get("balancedUpper")),
        )

    if is_ok_payload(stats):
        _check_calendar_date("stats", stats.get("calendarDate"), day)
        stress = _num(stats.get("averageStressLevel"))
        row.update(
            steps=_int(stats.get("totalSteps")),
            resting_hr=_num(stats.get("restingHeartRate")),
            stress_avg=stress if stress is not None and stress >= 0 else None,  # <0 = no data
            body_battery_charged=_int(stats.get("bodyBatteryChargedValue")),
        )
    if row.get("steps") is None and isinstance(steps, list):
        total = sum(_int(x.get("steps")) or 0 for x in steps if isinstance(x, Mapping))
        row["steps"] = total or None

    return {k: v for k, v in row.items() if v is not None}


def sleep_level_rows(sleep: Any, wake_date: date | str) -> list[dict[str, Any]]:
    """``sleep_levels`` rows from a sleep payload (empty unless it holds real sleep)."""
    if not is_real_sleep(sleep):
        return []
    day = _as_date(wake_date)
    rows = []
    for level in sleep.get("sleepLevels") or []:
        raw_level = _num(level.get("activityLevel"))
        start = parse_garmin_time(level.get("startGMT"), utc=True)
        end = parse_garmin_time(level.get("endGMT"), utc=True)
        if raw_level is None or start is None or end is None:
            continue
        stage = SLEEP_STAGES.get(int(raw_level))
        if stage is None or raw_level != int(raw_level):
            log.warning("unknown sleep activityLevel %r on %s skipped", raw_level, day)
            continue
        rows.append(
            {
                "start_utc": start,
                "end_utc": end,
                "wake_date": day,
                "level_raw": int(raw_level),
                "stage": stage,
            }
        )
    return rows


def hrv_reading_rows(hrv: Any, wake_date: date | str) -> list[dict[str, Any]]:
    """``hrv_readings`` rows from an HRV payload's ``hrvReadings``."""
    if not is_ok_payload(hrv):
        return []
    day = _as_date(wake_date)
    rows = []
    for reading in hrv.get("hrvReadings") or []:
        value = _num(reading.get("hrvValue"))
        ts = parse_garmin_time(reading.get("readingTimeGMT"), utc=True)
        if value is None or ts is None:
            continue
        rows.append({"ts": ts, "wake_date": day, "value_ms": value})
    return rows


@dataclass(frozen=True)
class WellnessResult:
    wake_date: date
    sleep_arrived: bool
    """True only for the call that stored this night's sleep for the first time."""
    sleep_synced_at: datetime | None
    """When this night's sleep first arrived (``None``: not synced yet)."""


_WELLNESS_VALUE_COLUMNS = (
    "sleep_score",
    "sleep_s",
    "deep_s",
    "light_s",
    "rem_s",
    "awake_s",
    "sleep_start_utc",
    "sleep_end_utc",
    "hrv_night_avg",
    "hrv_weekly_avg",
    "hrv_status",
    "hrv_balanced_low",
    "hrv_balanced_high",
    "resting_hr",
    "steps",
    "stress_avg",
    "body_battery_charged",
)


def _wellness_set(column: str) -> str:
    if column == "steps":
        # Today's step count is partial: never replace a larger stored value with a smaller one.
        return "steps = greatest(wellness_daily.steps, EXCLUDED.steps)"
    return f"{column} = coalesce(EXCLUDED.{column}, wellness_daily.{column})"


_WELLNESS_UPSERT = f"""
WITH prev AS (SELECT sleep_synced_at FROM wellness_daily WHERE wake_date = %(wake_date)s)
INSERT INTO wellness_daily (wake_date, {", ".join(_WELLNESS_VALUE_COLUMNS)},
                            sleep_synced_at, raw_sleep, raw_hrv, raw_stats)
VALUES (%(wake_date)s, {", ".join(f"%({c})s" for c in _WELLNESS_VALUE_COLUMNS)},
        %(sleep_synced_at)s, %(raw_sleep)s, %(raw_hrv)s, %(raw_stats)s)
ON CONFLICT (wake_date) DO UPDATE SET
  {", ".join(_wellness_set(c) for c in _WELLNESS_VALUE_COLUMNS)},
  sleep_synced_at = coalesce(wellness_daily.sleep_synced_at, EXCLUDED.sleep_synced_at),
  raw_sleep = CASE WHEN %(sleep_real)s THEN EXCLUDED.raw_sleep
                   ELSE coalesce(wellness_daily.raw_sleep, EXCLUDED.raw_sleep) END,
  raw_hrv = coalesce(EXCLUDED.raw_hrv, wellness_daily.raw_hrv),
  raw_stats = coalesce(EXCLUDED.raw_stats, wellness_daily.raw_stats)
RETURNING sleep_synced_at, (SELECT sleep_synced_at FROM prev) AS prev_synced_at
"""


def upsert_wellness(
    conn: Conn,
    wake_date: date | str,
    *,
    sleep: Any = None,
    hrv: Any = None,
    stats: Any = None,
    steps: Any = None,
    synced_at: datetime | None = None,
) -> WellnessResult:
    """Insert or update the ``wellness_daily`` row of ``wake_date`` from raw Garmin payloads.

    ``wake_date`` is the calendar day on which the athlete woke up; pass the payloads that
    Garmin returns for that same day (``get_sleep_data(d)``, ``get_hrv_data(d)``,
    ``get_stats(d)``, optionally ``get_steps_data(d)``). Any payload may be ``None`` or an error
    placeholder; it is then ignored. Rules:

    * a value that is missing from the new payloads never overwrites a stored one;
    * ``steps`` never decreases (today's count is partial);
    * ``sleep_synced_at`` is set once, when real sleep data first arrives (to ``synced_at``,
      default ``now()``; the legacy importer passes the original sync time);
    * ``sleep_levels`` and ``hrv_readings`` of the night are replaced when the new payload has
      them.

    Returns a :class:`WellnessResult`; ``sleep_arrived`` tells the sync poller to raise a
    ``sleep_arrived`` event.
    """
    day = _as_date(wake_date)
    row = wellness_row(day, sleep=sleep, hrv=hrv, stats=stats, steps=steps)
    sleep_real = is_real_sleep(sleep)
    params: dict[str, Any] = {c: row.get(c) for c in _WELLNESS_VALUE_COLUMNS}
    params.update(
        wake_date=day,
        sleep_real=sleep_real,
        sleep_synced_at=(synced_at or datetime.now(UTC)) if sleep_real else None,
        raw_sleep=to_jsonb(sleep) if is_ok_payload(sleep) else None,
        raw_hrv=to_jsonb(hrv) if is_ok_payload(hrv) else None,
        raw_stats=to_jsonb(stats) if is_ok_payload(stats) else None,
    )
    levels = sleep_level_rows(sleep, day)
    readings = hrv_reading_rows(hrv, day)

    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(_WELLNESS_UPSERT, params)
        result = cur.fetchone()
        assert result is not None
        synced, prev_synced = result
        if levels:
            cur.execute("DELETE FROM sleep_levels WHERE wake_date = %s", (day,))
            cur.executemany(
                "INSERT INTO sleep_levels (start_utc, end_utc, wake_date, level_raw, stage)"
                " VALUES (%(start_utc)s, %(end_utc)s, %(wake_date)s, %(level_raw)s, %(stage)s)"
                " ON CONFLICT (start_utc) DO UPDATE SET end_utc = EXCLUDED.end_utc,"
                " wake_date = EXCLUDED.wake_date, level_raw = EXCLUDED.level_raw,"
                " stage = EXCLUDED.stage",
                levels,
            )
        if readings:
            cur.execute("DELETE FROM hrv_readings WHERE wake_date = %s", (day,))
            cur.executemany(
                "INSERT INTO hrv_readings (ts, wake_date, value_ms)"
                " VALUES (%(ts)s, %(wake_date)s, %(value_ms)s)"
                " ON CONFLICT (ts) DO UPDATE SET wake_date = EXCLUDED.wake_date,"
                " value_ms = EXCLUDED.value_ms",
                readings,
            )
    return WellnessResult(
        wake_date=day,
        sleep_arrived=sleep_real and prev_synced is None and synced is not None,
        sleep_synced_at=synced,
    )


# Personal records -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonalRecord:
    key: str  # e.g. "garmin_3"
    label: str  # e.g. "5k"
    value: float | None  # seconds for time records, metres or steps otherwise
    activity_id: int | None
    activity_name: str | None
    date: date | None
    source: str  # e.g. "garmin_pr", "computed"


def garmin_personal_records(payload: Iterable[Mapping[str, Any]]) -> list[PersonalRecord]:
    """Map ``garminconnect``'s ``get_personal_record()`` list to :class:`PersonalRecord` rows."""
    out = []
    for item in payload:
        type_id = item.get("typeId")
        if type_id is None:
            continue
        local = item.get("activityStartDateTimeLocalFormatted") or ""
        activity_id = item.get("activityId")
        out.append(
            PersonalRecord(
                key=f"garmin_{type_id}",
                label=PR_LABELS.get(int(type_id), f"type_{type_id}"),
                value=_num(item.get("value")),
                activity_id=int(activity_id) if activity_id else None,
                activity_name=item.get("activityName"),
                date=date.fromisoformat(local[:10]) if len(local) >= 10 else None,
                source="garmin_pr",
            )
        )
    return out


def upsert_personal_records(conn: Conn, records: Iterable[PersonalRecord]) -> int:
    """Insert or replace personal records by ``key``. Returns the number of rows given."""
    rows = [
        {
            "key": r.key,
            "label": r.label,
            "value": r.value,
            "activity_id": r.activity_id,
            "activity_name": r.activity_name,
            "date": r.date,
            "source": r.source,
        }
        for r in records
    ]
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO personal_records"
            " (key, label, value, activity_id, activity_name, date, source)"
            " VALUES (%(key)s, %(label)s, %(value)s, %(activity_id)s, %(activity_name)s,"
            " %(date)s, %(source)s)"
            " ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, value = EXCLUDED.value,"
            " activity_id = EXCLUDED.activity_id, activity_name = EXCLUDED.activity_name,"
            " date = EXCLUDED.date, source = EXCLUDED.source",
            rows,
        )
    return len(rows)


# Events ---------------------------------------------------------------------------------------


def insert_event(
    conn: Conn,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
) -> int | None:
    """Insert an event; returns its id, or ``None`` if ``dedupe_key`` already exists.

    Events without a ``dedupe_key`` are always inserted. See the event table in
    ``docs/ARCHITECTURE.md`` for kinds, keys and payloads, or use the helpers below.
    """
    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "INSERT INTO events (kind, payload, dedupe_key) VALUES (%s, %s, %s)"
            " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (kind, to_jsonb(dict(payload or {})), dedupe_key),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def insert_sleep_arrived(conn: Conn, wake_date: date | str) -> int | None:
    """``sleep_arrived`` event, deduplicated by ``sleep:<wake_date>``."""
    day = _as_date(wake_date).isoformat()
    return insert_event(conn, "sleep_arrived", {"wake_date": day}, dedupe_key=f"sleep:{day}")


def insert_new_activity(conn: Conn, activity_id: int, type_key: str, is_run: bool) -> int | None:
    """``new_activity`` event, deduplicated by ``activity:<id>``."""
    return insert_event(
        conn,
        "new_activity",
        {"activity_id": int(activity_id), "type": type_key, "is_run": bool(is_run)},
        dedupe_key=f"activity:{int(activity_id)}",
    )


# Sync state -----------------------------------------------------------------------------------


def get_sync_state(conn: Conn, key: str, default: Any = None) -> Any:
    """Return the JSON value stored under ``key``, or ``default``."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
        row = cur.fetchone()
    return default if row is None else row[0]


def set_sync_state(conn: Conn, key: str, value: Any) -> None:
    """Store a JSON-serialisable ``value`` under ``key`` (dates become ISO strings)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_state (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, to_jsonb(value)),
        )
