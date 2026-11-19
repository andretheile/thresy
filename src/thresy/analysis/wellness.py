"""HRV, resting heart rate and sleep over the last N days (keyed by wake date)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from thresy.analysis.data import Conn

COLUMNS = (
    "wake_date",
    "sleep_s",
    "sleep_score",
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


def _mean(values: list[Any], digits: int = 1) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(float(np.mean(vals)), digits) if vals else None


def trend(conn: Conn, end: date, days: int = 28) -> dict[str, Any]:
    """Daily rows from ``end - days + 1`` to ``end`` and a summary.

    ``below_range`` marks nights whose HRV is below Garmin's balanced range (``hrv_balanced_low``
    of the same row); ``above_range`` likewise. The summary compares the means of the last 7 days
    with the 7 days before.
    """
    start = end - timedelta(days=days - 1)
    rows = conn.execute(
        f"SELECT {', '.join(COLUMNS)} FROM wellness_daily"
        " WHERE wake_date BETWEEN %s AND %s ORDER BY wake_date",
        (start, end),
    ).fetchall()
    by_day = {r["wake_date"]: dict(r) for r in rows}
    daily = []
    for n in range(days):
        day = start + timedelta(days=n)
        r = by_day.get(day, {"wake_date": day})
        hrv, low, high = (
            r.get("hrv_night_avg"),
            r.get("hrv_balanced_low"),
            r.get("hrv_balanced_high"),
        )
        r["below_range"] = bool(hrv is not None and low is not None and hrv < low)
        r["above_range"] = bool(hrv is not None and high is not None and hrv > high)
        daily.append({k: r.get(k) for k in (*COLUMNS, "below_range", "above_range")})

    last7 = [d for d in daily if d["wake_date"] > end - timedelta(days=7)]
    prev7 = [
        d for d in daily if end - timedelta(days=14) < d["wake_date"] <= end - timedelta(days=7)
    ]

    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "hrv": _mean([d["hrv_night_avg"] for d in items]),
            "resting_hr": _mean([d["resting_hr"] for d in items]),
            "sleep_s": _mean([d["sleep_s"] for d in items], 0),
            "sleep_score": _mean([d["sleep_score"] for d in items]),
            "nights": sum(1 for d in items if d["sleep_s"]),
        }

    latest = next((d for d in reversed(daily) if d["hrv_night_avg"] is not None), None)
    return {
        "from": start,
        "to": end,
        "days": days,
        "summary": {
            "last7": block(last7),
            "prev7": block(prev7),
            "period": block(daily),
            "nights_below_range": sum(d["below_range"] for d in daily),
            "nights_above_range": sum(d["above_range"] for d in daily),
            "days_without_sleep": sum(1 for d in daily if not d["sleep_s"]),
            "latest_hrv": latest,
        },
        "daily": daily,
    }
