"""Load the synthetic demo athlete (thresy.web.demo) into a database, for the tests that
compare the database path of the dashboard with the demo path, and for timing the pages.

Uses a read/write (``coach_app``) connection; the dashboard itself never writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from psycopg.types.json import Jsonb

from thresy.web.demo import DemoData, DemoSource

ACTIVITY_COLUMNS = (
    "id", "start_utc", "start_local", "local_date", "type", "is_run", "name", "distance_m",
    "duration_s", "moving_s", "elapsed_s", "avg_hr", "max_hr", "elev_gain_m", "elev_loss_m",
    "avg_cadence_spm", "avg_power_w", "vo2max", "aerobic_te", "anaerobic_te", "training_load",
    "calories", "location", "has_records", "source",
)  # fmt: skip
RECORD_COLUMNS = (
    "activity_id", "ts", "elapsed_s", "lat", "lon", "alt_m", "dist_m", "speed_mps", "hr",
    "cadence_spm", "power_w", "resp_rate", "temp_c",
)  # fmt: skip
LAP_COLUMNS = (
    "activity_id", "lap_no", "start_utc", "duration_s", "elapsed_s", "dist_m", "avg_speed_mps",
    "max_speed_mps", "avg_hr", "max_hr", "avg_cadence_spm", "avg_power_w", "ascent_m",
    "descent_m", "avg_temp_c", "intensity", "trigger", "wkt_step",
)  # fmt: skip
WELLNESS_COLUMNS = (
    "wake_date", "sleep_score", "sleep_s", "deep_s", "light_s", "rem_s", "awake_s",
    "sleep_start_utc", "sleep_end_utc", "hrv_night_avg", "hrv_weekly_avg", "hrv_status",
    "hrv_balanced_low", "hrv_balanced_high", "resting_hr", "steps", "stress_avg",
    "body_battery_charged", "sleep_synced_at",
)  # fmt: skip


def _insert(conn: Any, table: str, columns: Iterable[str], rows: list[dict[str, Any]]) -> None:
    cols = list(columns)
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            [tuple(r.get(c) for c in cols) for r in rows],
        )


def load(
    conn: Any, scenario: str = "full", lang: str = "en", *, records: int | None = None
) -> DemoData:
    """Insert the demo athlete. ``records`` limits the per-second data to the most recent
    that many runs (all runs that have it by default)."""
    source = DemoSource(scenario, lang)
    data = source.data
    _insert(conn, "activities", ACTIVITY_COLUMNS, data.activities)
    with_records = [
        a["id"]
        for a in sorted(data.activities, key=lambda a: a["start_utc"], reverse=True)
        if a["has_records"]
    ]
    if records is not None:
        keep = set(with_records[:records])
        if dropped := [i for i in with_records if i not in keep]:
            conn.execute("UPDATE activities SET has_records = false WHERE id = ANY(%s)", (dropped,))
        with_records = with_records[:records]
    with conn.cursor() as cur:
        for activity_id in with_records:
            raw, laps = source.raw_records(activity_id)
            with cur.copy(
                f"COPY activity_records ({', '.join(RECORD_COLUMNS)}) FROM STDIN"
            ) as copy:
                for row in raw.itertuples(index=False):
                    values = row._asdict()
                    copy.write_row(
                        [
                            activity_id,
                            *(
                                None if _missing(values[c]) else _plain(values[c])
                                for c in RECORD_COLUMNS[1:]
                            ),
                        ]
                    )
            _insert(
                conn,
                "activity_laps",
                LAP_COLUMNS,
                [{"activity_id": activity_id, **lap} for lap in laps],
            )
    _insert(conn, "wellness_daily", WELLNESS_COLUMNS, list(data.wellness.values()))
    levels = [{**lv, "wake_date": day} for day, items in data.levels.items() for lv in items]
    _insert(
        conn, "sleep_levels", ("start_utc", "end_utc", "wake_date", "level_raw", "stage"), levels
    )
    plan_rows = [
        {**w, "target": Jsonb(w["target"]) if w["target"] else None} for w in data.workouts
    ]
    _insert(
        conn,
        "planned_workouts",
        (
            "id",
            "date",
            "title",
            "kind",
            "description",
            "target",
            "status",
            "activity_id",
            "created_at",
            "updated_at",
        ),
        plan_rows,
    )
    race_rows = [{**r, "result": Jsonb(r["result"]) if r["result"] else None} for r in data.races]
    _insert(
        conn,
        "races",
        ("id", "date", "name", "distance_km", "elev_m", "location", "start_time_local", "priority",
         "goal", "status", "activity_id", "result", "url", "notes"),
        race_rows,
    )  # fmt: skip
    _insert(
        conn,
        "segments",
        list(data.segments[0].as_dict()) if data.segments else [],
        [s.as_dict() for s in data.segments],
    )
    effort_rows = [{"segment_key": key, **e} for key, items in data.efforts.items() for e in items]
    _insert(
        conn,
        "segment_efforts",
        ("segment_key", "activity_id", "start_utc", "duration_s", "dist_m", "gain_m", "avg_hr",
         "max_hr", "hr_last60", "pace_s_per_km"),
        effort_rows,
    )  # fmt: skip
    if data.messages:
        session = uuid.uuid4()
        conn.execute(
            "INSERT INTO agent_sessions (id, chat_id, runner, day) VALUES (%s, 'demo', 'fake', %s)",
            (session, data.today),
        )
        for m in data.messages:
            run = conn.execute(
                "INSERT INTO agent_runs (session_id, trigger, runner, status, started_at)"
                " VALUES (%s, %s, 'fake', 'done', %s) RETURNING id",
                (session, m["trigger"], m["ts"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO messages (ts, chat_id, direction, kind, text, transport, run_id)"
                " VALUES (%s, 'demo', 'out', 'text', %s, 'fake', %s)",
                (m["ts"], m["text"], run["id"]),
            )
    for key, value in data.state.items():
        if value:
            conn.execute("INSERT INTO sync_state (key, value) VALUES (%s, %s)", (key, Jsonb(value)))
    for table in ("planned_workouts", "races"):
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
            f" coalesce((SELECT max(id) FROM {table}), 1))"
        )
    return data


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and value != value)


def _plain(value: Any) -> Any:
    return value.item() if hasattr(value, "item") else value
