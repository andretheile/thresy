"""Where the dashboard's data comes from: the database (:class:`DbSource`) or the synthetic
demo athlete (:class:`thresy.web.demo.DemoSource`), behind one interface (:class:`Source`).

:class:`DbSource` answers through the same library functions as the ``coach`` commands
(``thresy.analysis``, ``thresy.tools.plan``, ``thresy.tools.races``), so the page and the coach
never disagree. Where no such function exists, the small read helpers below fill the gap
(marked "web helper"); they only read, and only summary tables, never the per-second samples
of more than one activity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from thresy.analysis.data import Activity, Conn
from thresy.analysis.segments import SegmentDef


class Source(Protocol):
    """Everything the pages read. Return shapes are those of the library functions named."""

    zone: ZoneInfo
    demo: bool

    def now(self) -> datetime: ...
    def today(self) -> date: ...

    def night(self, wake_date: date) -> dict[str, Any]:
        """``thresy.analysis.night.night``."""

    def wellness(self, end: date, days: int) -> dict[str, Any]:
        """``thresy.analysis.wellness.trend``."""

    def weeks(self, today: date, n_weeks: int) -> dict[str, Any]:
        """``thresy.analysis.weeks.weeks`` (runs only)."""

    def plan_week(self, monday: date, weeks: int, today: date) -> dict[str, Any]:
        """``thresy.tools.plan.week``."""

    def runs(
        self, *, since: date | None = None, until: date | None = None, limit: int | None = None
    ) -> list[Activity]:
        """``thresy.analysis.data.find_activities`` (runs, newest first)."""

    def run_years(self) -> list[dict[str, Any]]:
        """Web helper: ``[{year, runs, distance_m}]``, newest first."""

    def activity(self, activity_id: int) -> Activity | None: ...
    def activity_extra(self, activity_id: int) -> dict[str, Any]:
        """Web helper: summary columns the ``Activity`` dataclass leaves out."""

    def records(self, activity_id: int) -> pd.DataFrame:
        """``thresy.analysis.data.load_records``."""

    def laps(self, activity_id: int, first_ts: pd.Timestamp | None) -> pd.DataFrame:
        """``thresy.analysis.data.load_laps``."""

    def daily_distance(self, since: date, until: date) -> list[tuple[date, float]]:
        """Web helper: running metres per local date (days with runs only)."""

    def races(self, today: date) -> list[dict[str, Any]]:
        """``thresy.tools.races.listing(which="all")``."""

    def segments(self) -> list[SegmentDef]:
        """``thresy.analysis.segments.list_segments``."""

    def segment_history(self, seg: SegmentDef) -> dict[str, Any]:
        """``thresy.analysis.segments.history`` over the cached efforts."""

    def activity_efforts(self, activity_id: int) -> list[dict[str, Any]]:
        """Web helper: cached segment efforts of one activity, with the segment's name."""

    def planned_for(self, activity_id: int) -> list[dict[str, Any]]:
        """Web helper: planned workouts linked to one activity."""

    def coach_message(self) -> dict[str, Any] | None:
        """Web helper: the latest morning briefing or run debrief that was sent."""

    def state(self) -> dict[str, Any]:
        """Web helper: ``sync_status``, ``garmin_auth``, ``garmin_rate_limit`` and
        ``backup_status`` from ``sync_state``."""

    def counts(self) -> dict[str, Any]:
        """Web helper: how much the database holds."""


# -- the database ----------------------------------------------------------------------------------


class DbSource:
    """Reads one connection (role ``coach_ro``); one instance per request."""

    demo = False

    def __init__(self, conn: Conn, zone: ZoneInfo, *, now: datetime | None = None) -> None:
        self.conn = conn
        self.zone = zone
        self._now = now

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def today(self) -> date:
        return self.now().astimezone(self.zone).date()

    def night(self, wake_date: date) -> dict[str, Any]:
        from thresy.analysis import night

        return night.night(self.conn, wake_date, self.zone)

    def wellness(self, end: date, days: int) -> dict[str, Any]:
        from thresy.analysis import wellness

        return wellness.trend(self.conn, end, days)

    def weeks(self, today: date, n_weeks: int) -> dict[str, Any]:
        from thresy.analysis import weeks

        return weeks.weeks(self.conn, today, n_weeks)

    def plan_week(self, monday: date, weeks: int, today: date) -> dict[str, Any]:
        from thresy.tools import plan

        return plan.week(self.conn, monday, weeks=weeks, today=today)

    def runs(
        self, *, since: date | None = None, until: date | None = None, limit: int | None = None
    ) -> list[Activity]:
        from thresy.analysis import data

        return data.find_activities(self.conn, since=since, until=until, limit=limit)

    def run_years(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT extract(year FROM local_date)::int AS year, count(*) AS runs,"
            " coalesce(sum(distance_m), 0) AS distance_m"
            " FROM activities WHERE is_run GROUP BY 1 ORDER BY 1 DESC"
        ).fetchall()
        return [
            {"year": int(r["year"]), "runs": int(r["runs"]), "distance_m": float(r["distance_m"])}
            for r in rows
        ]

    def activity(self, activity_id: int) -> Activity | None:
        from thresy.analysis import data

        return data.get_activity(self.conn, activity_id)

    def activity_extra(self, activity_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT aerobic_te, anaerobic_te, training_load, calories, vo2max"
            " FROM activities WHERE id = %s",
            (activity_id,),
        ).fetchone()
        return dict(row) if row else {}

    def records(self, activity_id: int) -> pd.DataFrame:
        from thresy.analysis import data

        return data.load_records(self.conn, activity_id)

    def laps(self, activity_id: int, first_ts: pd.Timestamp | None) -> pd.DataFrame:
        from thresy.analysis import data

        return data.load_laps(self.conn, activity_id, first_ts)

    def daily_distance(self, since: date, until: date) -> list[tuple[date, float]]:
        rows = self.conn.execute(
            "SELECT local_date, sum(distance_m) AS m FROM activities"
            " WHERE is_run AND local_date BETWEEN %s AND %s AND distance_m > 0"
            " GROUP BY local_date ORDER BY local_date",
            (since, until),
        ).fetchall()
        return [(r["local_date"], float(r["m"])) for r in rows]

    def races(self, today: date) -> list[dict[str, Any]]:
        from thresy.tools import races

        return races.listing(self.conn, today=today, which="all")

    def segments(self) -> list[SegmentDef]:
        from thresy.analysis import segments

        return segments.list_segments(self.conn)

    def segment_history(self, seg: SegmentDef) -> dict[str, Any]:
        from thresy.analysis import segments

        cached = segments.cached_efforts(self.conn, seg.key)
        efforts = [{"activity_id": aid, **e} for aid, items in cached.items() for e in items]
        efforts.sort(key=lambda e: e["start_utc"])
        return segments.history(self.conn, seg, efforts)

    def activity_efforts(self, activity_id: int) -> list[dict[str, Any]]:
        from thresy.analysis import segments

        rows = self.conn.execute(
            "SELECT e.segment_key, s.name AS segment, e.start_utc, e.duration_s, e.dist_m,"
            " e.gain_m, e.avg_hr, e.max_hr, e.hr_last60, e.pace_s_per_km"
            " FROM segment_efforts e JOIN segments s ON s.key = e.segment_key"
            " WHERE e.activity_id = %s ORDER BY e.start_utc",
            (activity_id,),
        ).fetchall()
        return [segments.with_derived(dict(r)) for r in rows]

    def planned_for(self, activity_id: int) -> list[dict[str, Any]]:
        from thresy.tools import plan

        rows = self.conn.execute(
            f"SELECT {plan.COLUMNS} FROM planned_workouts WHERE activity_id = %s ORDER BY id",
            (activity_id,),
        ).fetchall()
        return [{**dict(r), "target": dict(r["target"] or {})} for r in rows]

    def coach_message(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT m.ts, m.text, r.trigger FROM messages m JOIN agent_runs r ON r.id = m.run_id"
            " WHERE m.direction = 'out' AND m.kind = 'text' AND m.text IS NOT NULL"
            " AND (r.trigger LIKE %s OR r.trigger LIKE %s)"
            " ORDER BY m.ts DESC, m.id DESC LIMIT 1",
            ("%morning_briefing%", "%post_run%"),
        ).fetchone()
        return dict(row) if row else None

    def state(self) -> dict[str, Any]:
        keys = ("sync_status", "garmin_auth", "garmin_rate_limit", "backup_status")
        rows = self.conn.execute(
            "SELECT key, value FROM sync_state WHERE key = ANY(%s)", (list(keys),)
        ).fetchall()
        found = {r["key"]: r["value"] for r in rows}
        return {k: dict(found[k]) if isinstance(found.get(k), dict) else {} for k in keys}

    def counts(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT count(*) FILTER (WHERE is_run) AS runs,"
            " count(*) FILTER (WHERE is_run AND has_records) AS with_records,"
            " min(local_date) FILTER (WHERE is_run) AS first_run,"
            " max(local_date) FILTER (WHERE is_run) AS last_run FROM activities"
        ).fetchone()
        nights = self.conn.execute(
            "SELECT count(*) AS n FROM wellness_daily WHERE sleep_s > 0"
        ).fetchone()
        return {**dict(row), "nights": int(nights["n"]) if nights else 0}


def parse_utc(value: Any) -> datetime | None:
    """A timestamp from ``sync_state`` (ISO text) or the database, as aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())
