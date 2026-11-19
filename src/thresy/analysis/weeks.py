"""Weekly training volume and year-to-date totals (from the activity summaries).

Weeks run Monday to Sunday by the activities' local date. Time is moving time, falling back to
the timer duration when Garmin reports no moving time; ascent is Garmin's elevation gain.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from thresy.analysis.data import Conn

_AGG = """
SELECT count(*) AS runs,
       coalesce(sum(distance_m), 0) AS distance_m,
       coalesce(sum(coalesce(nullif(moving_s, 0), duration_s)), 0) AS time_s,
       coalesce(sum(elev_gain_m), 0) AS ascent_m,
       max(distance_m) AS longest_m
FROM activities
WHERE local_date BETWEEN %(from)s AND %(to)s AND (is_run OR NOT %(runs_only)s)
"""


def _totals(conn: Conn, start: date, end: date, runs_only: bool) -> dict[str, Any]:
    row = conn.execute(_AGG, {"from": start, "to": end, "runs_only": runs_only}).fetchone()
    return {
        "from": start,
        "to": end,
        "runs": int(row["runs"]),
        "distance_km": round(float(row["distance_m"]) / 1000, 1),
        "time_s": round(float(row["time_s"])),
        "ascent_m": round(float(row["ascent_m"])),
        "longest_km": round(float(row["longest_m"]) / 1000, 1) if row["longest_m"] else None,
    }


def weeks(conn: Conn, today: date, n_weeks: int = 12, *, runs_only: bool = True) -> dict[str, Any]:
    """The last ``n_weeks`` weeks (the current one included, possibly partial) and the totals
    of this year and of the same period last year."""
    monday = today - timedelta(days=today.weekday())
    out = []
    for k in range(n_weeks - 1, -1, -1):
        start = monday - timedelta(weeks=k)
        end = min(start + timedelta(days=6), today)
        out.append({"week": start.isocalendar().week, **_totals(conn, start, end, runs_only)})
    year_start = date(today.year, 1, 1)
    try:
        same_day_last_year = today.replace(year=today.year - 1)
    except ValueError:  # 29 February
        same_day_last_year = today.replace(year=today.year - 1, day=28)
    return {
        "today": today,
        "runs_only": runs_only,
        "weeks": out,
        "ytd": _totals(conn, year_start, today, runs_only),
        "ytd_last_year": _totals(conn, date(today.year - 1, 1, 1), same_day_last_year, runs_only),
    }
