"""One night: sleep stages and the HRV readings, keyed by wake date.

Stages come from ``sleep_levels`` (Garmin activityLevel 0 = deep, 1 = light, 2 = rem,
3 = awake; the old bot treated 2 as awake, which is wrong). HRV readings (about every 5 minutes)
are assigned the stage of the sleep level they fall into. Reported:

* ``avg_all``: mean of all readings of the night;
* ``avg_asleep``: mean of the readings outside awake periods;
* ``avg_first_3h``: mean of the readings in the first three hours after sleep start, outside
  awake periods (sleep start = ``wellness_daily.sleep_start_utc``, else the first sleep level,
  else the first reading);
* ``min``/``max`` with their local times, the mean per stage, and the awake periods
  (consecutive awake levels merged) with their local start, end and duration.

Local times use the offset Garmin reports for the night (``sleepStartTimestampLocal`` minus
``sleepStartTimestampGMT``), falling back to ``COACH_TZ``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from thresy.analysis.data import Conn

FIRST_HOURS = 3


def _offset(row: dict[str, Any] | None, at: datetime | None, tz: ZoneInfo) -> timedelta:
    raw = (row or {}).get("raw_sleep") or {}
    dto = raw.get("dailySleepDTO") or {} if isinstance(raw, dict) else {}
    local, gmt = dto.get("sleepStartTimestampLocal"), dto.get("sleepStartTimestampGMT")
    if isinstance(local, int | float) and isinstance(gmt, int | float):
        return timedelta(milliseconds=local - gmt)
    when = at or datetime.now(UTC)
    return when.astimezone(tz).utcoffset() or timedelta(0)


def latest_synced(conn: Conn) -> date | None:
    row = conn.execute(
        "SELECT max(wake_date) AS d FROM wellness_daily WHERE sleep_s > 0"
    ).fetchone()
    return row["d"] if row else None


def night(conn: Conn, wake_date: date, tz: ZoneInfo) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM wellness_daily WHERE wake_date = %s", (wake_date,)).fetchone()
    levels = conn.execute(
        "SELECT start_utc, end_utc, level_raw, stage FROM sleep_levels WHERE wake_date = %s"
        " ORDER BY start_utc",
        (wake_date,),
    ).fetchall()
    readings = conn.execute(
        "SELECT ts, value_ms FROM hrv_readings WHERE wake_date = %s ORDER BY ts", (wake_date,)
    ).fetchall()
    synced = bool(row and row["sleep_s"])
    out: dict[str, Any] = {
        "wake_date": wake_date,
        "synced": synced,
        "sleep_synced_at": row["sleep_synced_at"] if row else None,
    }
    if not synced and not readings:
        out["latest_synced"] = latest_synced(conn)
        return out

    sleep_start = (row or {}).get("sleep_start_utc") or (
        levels[0]["start_utc"] if levels else readings[0]["ts"]
    )
    sleep_end = (row or {}).get("sleep_end_utc") or (levels[-1]["end_utc"] if levels else None)
    offset = _offset(row, sleep_start, tz)

    def local(ts: datetime | None) -> datetime | None:
        return None if ts is None else ts.astimezone(UTC).replace(tzinfo=None) + offset

    # awake periods: consecutive awake levels merged
    awake: list[dict[str, Any]] = []
    for lv in levels:
        if lv["stage"] != "awake":
            continue
        if awake and awake[-1]["_end"] >= lv["start_utc"]:
            awake[-1]["_end"] = max(awake[-1]["_end"], lv["end_utc"])
        else:
            awake.append({"_start": lv["start_utc"], "_end": lv["end_utc"]})
    awake_out = [
        {
            "start_local": local(a["_start"]),
            "end_local": local(a["_end"]),
            "duration_s": round((a["_end"] - a["_start"]).total_seconds()),
        }
        for a in awake
    ]

    def stage_at(ts: datetime) -> str | None:
        for lv in levels:
            if lv["start_utc"] <= ts < lv["end_utc"]:
                return lv["stage"]
        return None

    points = [
        {"ts": r["ts"], "local": local(r["ts"]), "value_ms": float(r["value_ms"]),
         "stage": stage_at(r["ts"])}
        for r in readings
    ]  # fmt: skip

    def avg(values: list[float]) -> float | None:
        return round(float(np.mean(values)), 1) if values else None

    first_end = sleep_start + timedelta(hours=FIRST_HOURS)
    asleep = [p["value_ms"] for p in points if p["stage"] != "awake"]
    first = [
        p["value_ms"]
        for p in points
        if sleep_start <= p["ts"] < first_end and p["stage"] != "awake"
    ]
    per_stage = {
        stage: avg([p["value_ms"] for p in points if p["stage"] == stage])
        for stage in ("deep", "light", "rem", "awake")
    }
    lo = min(points, key=lambda p: p["value_ms"]) if points else None
    hi = max(points, key=lambda p: p["value_ms"]) if points else None
    stage_totals: dict[str, float] = {}
    for lv in levels:
        stage_totals[lv["stage"]] = (
            stage_totals.get(lv["stage"], 0.0) + (lv["end_utc"] - lv["start_utc"]).total_seconds()
        )
    out.update(
        sleep={
            "start_local": local(sleep_start),
            "end_local": local(sleep_end),
            "sleep_s": row["sleep_s"] if row else None,
            "score": row["sleep_score"] if row else None,
            "deep_s": row["deep_s"] if row else None,
            "light_s": row["light_s"] if row else None,
            "rem_s": row["rem_s"] if row else None,
            "awake_s": row["awake_s"] if row else None,
            "stages_from_levels_s": {k: round(v) for k, v in stage_totals.items()},
        },
        garmin_hrv={
            "night_avg": row["hrv_night_avg"] if row else None,
            "weekly_avg": row["hrv_weekly_avg"] if row else None,
            "status": row["hrv_status"] if row else None,
            "balanced_low": row["hrv_balanced_low"] if row else None,
            "balanced_high": row["hrv_balanced_high"] if row else None,
        },
        resting_hr=row["resting_hr"] if row else None,
        hrv={
            "n": len(points),
            "n_awake": sum(1 for p in points if p["stage"] == "awake"),
            "avg_all": avg([p["value_ms"] for p in points]),
            "avg_asleep": avg(asleep),
            "avg_first_3h": avg(first),
            "n_first_3h": len(first),
            "min": {"value_ms": lo["value_ms"], "local": lo["local"]} if lo else None,
            "max": {"value_ms": hi["value_ms"], "local": hi["local"]} if hi else None,
            "per_stage": per_stage,
        },
        awake_periods=awake_out,
        levels=[
            {
                "start_local": local(lv["start_utc"]),
                "end_local": local(lv["end_utc"]),
                "stage": lv["stage"],
            }
            for lv in levels
        ],
        readings=[
            {"local": p["local"], "value_ms": p["value_ms"], "stage": p["stage"]} for p in points
        ],
    )
    return out
