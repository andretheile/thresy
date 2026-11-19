"""Synthetic Garmin payloads shaped like the real API responses. All values are made up."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any


def activity(
    activity_id: int = 1001,
    *,
    type_key: str = "running",
    start_local: str = "2030-01-02 07:00:00",
    start_gmt: str = "2030-01-02 06:00:00",
    distance: float = 10000.0,
    duration: float | None = 3000.0,
    moving: float | None = 2950.0,
    name: str = "Morning Run",
    **extra: Any,
) -> dict[str, Any]:
    """An item of garminconnect's get_activities_by_date()."""
    payload: dict[str, Any] = {
        "activityId": activity_id,
        "activityName": name,
        "startTimeLocal": start_local,
        "startTimeGMT": start_gmt,
        "activityType": {"typeId": 1, "typeKey": type_key, "parentTypeId": 17},
        "distance": distance,
        "duration": duration,
        "elapsedDuration": (duration or 0) + 60,
        "movingDuration": moving,
        "averageHR": 140.0,
        "maxHR": 170.0,
        "elevationGain": 100.0,
        "elevationLoss": 95.0,
        "averageRunningCadenceInStepsPerMinute": 170.0,
        "avgPower": 250.0,
        "vO2MaxValue": 50.0,
        "aerobicTrainingEffect": 3.0,
        "anaerobicTrainingEffect": 1.0,
        "activityTrainingLoad": 100.0,
        "calories": 600.0,
        "locationName": "Sample Town",
        "beginTimestamp": 1893564000000,
    }
    payload.update(extra)
    return payload


def _gmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0")


def sleep(
    wake_date: date,
    *,
    levels: list[tuple[int, int]] | None = None,
    score: int = 80,
    synced: bool = True,
) -> dict[str, Any]:
    """get_sleep_data(wake_date). ``levels`` is a list of (activityLevel, minutes).

    Stage totals in the DTO are derived from ``levels`` using the mapping found in real data
    (0 deep, 1 light, 2 rem, 3 awake). ``synced=False`` gives the placeholder Garmin returns
    before the watch has uploaded the night.
    """
    if not synced:
        return {"dailySleepDTO": {"calendarDate": wake_date.isoformat(), "id": None}}
    levels = levels or [(1, 30), (0, 60), (1, 90), (2, 30), (3, 10), (1, 120), (2, 60)]
    start = datetime.combine(wake_date, datetime.min.time(), tzinfo=UTC) - timedelta(hours=2)
    rows, totals, t = [], {0: 0, 1: 0, 2: 0, 3: 0}, start
    for level, minutes in levels:
        end = t + timedelta(minutes=minutes)
        rows.append({"startGMT": _gmt(t), "endGMT": _gmt(end), "activityLevel": float(level)})
        totals[level] = totals.get(level, 0) + minutes * 60
        t = end
    asleep = totals[0] + totals[1] + totals[2]
    return {
        "dailySleepDTO": {
            "id": 1,
            "calendarDate": wake_date.isoformat(),
            "sleepTimeSeconds": asleep,
            "deepSleepSeconds": totals[0],
            "lightSleepSeconds": totals[1],
            "remSleepSeconds": totals[2],
            "awakeSleepSeconds": totals[3],
            "sleepStartTimestampGMT": int(start.timestamp() * 1000),
            "sleepEndTimestampGMT": int(t.timestamp() * 1000),
            "sleepScores": {"overall": {"value": score, "qualifierKey": "GOOD"}},
        },
        "sleepLevels": rows,
    }


def hrv(wake_date: date, *, values: list[int] | None = None, night_avg: int = 60) -> dict[str, Any]:
    """get_hrv_data(wake_date) with 5-minute readings."""
    values = values or [55, 60, 65, 62]
    start = datetime.combine(wake_date, datetime.min.time(), tzinfo=UTC) - timedelta(hours=1)
    readings = [
        {
            "hrvValue": v,
            "readingTimeGMT": _gmt(start + timedelta(minutes=5 * i)),
            "readingTimeLocal": _gmt(start + timedelta(minutes=5 * i)),
        }
        for i, v in enumerate(values)
    ]
    return {
        "hrvSummary": {
            "calendarDate": wake_date.isoformat(),
            "lastNightAvg": night_avg,
            "weeklyAvg": 58,
            "status": "BALANCED",
            "baseline": {"lowUpper": 45, "balancedLow": 50, "balancedUpper": 70},
        },
        "hrvReadings": readings,
    }


def stats(day: date, *, steps: int | None = 8000, resting_hr: int = 50) -> dict[str, Any]:
    """get_stats(day)."""
    return {
        "calendarDate": day.isoformat(),
        "totalSteps": steps,
        "restingHeartRate": resting_hr,
        "averageStressLevel": 30,
        "bodyBatteryChargedValue": 60,
    }
