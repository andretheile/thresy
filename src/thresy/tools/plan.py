"""``coach plan``: the training plan in ``planned_workouts``, next to what was actually run.

The agent writes the plan with its own database role (``coach_agent`` may INSERT, UPDATE and
DELETE ``planned_workouts``) and reads ``activities`` to put the runs of each day next to the
plan. ``target`` is a JSON object with optional keys ``distance_km``, ``duration_min``,
``pace`` (text such as ``"4:10-4:20"``), ``hr`` (text such as ``"<150"``) and ``ascent_m``,
plus anything else the agent wants to keep (``note`` for how it went, ``moved_from`` /
``moved_to`` for the bookkeeping of a moved workout).

Status: ``planned``, ``done``, ``skipped``, ``moved`` (the workout was moved to another day
after its own day had arrived; the copy on the new day carries the plan on).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from thresy.analysis import fmt
from thresy.tools.dates import relative, short

STATUSES = ("planned", "done", "skipped", "moved")
KINDS = (
    "easy",
    "recovery",
    "long",
    "tempo",
    "threshold",
    "intervals",
    "hills",
    "fartlek",
    "strides",
    "race",
    "cross",
    "strength",
    "rest",
    "other",
)
TARGET_KEYS = ("distance_km", "duration_min", "pace", "hr", "ascent_m")
COLUMNS = "id, date, title, kind, description, target, status, activity_id, created_at, updated_at"


class PlanError(ValueError):
    """The request cannot be carried out; the message says what to do instead."""


def check_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    value = kind.strip().lower()
    if value not in KINDS:
        raise PlanError(f"unknown kind {kind!r}; use one of: {', '.join(KINDS)}")
    return value


def check_status(status: str) -> str:
    value = status.strip().lower()
    if value not in STATUSES:
        raise PlanError(f"unknown status {status!r}; use one of: {', '.join(STATUSES)}")
    return value


def build_target(
    *,
    distance_km: float | None = None,
    duration_min: float | None = None,
    pace: str | None = None,
    hr: str | None = None,
    ascent_m: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The target object from the command-line options (only the keys that were given)."""
    target: dict[str, Any] = dict(extra or {})
    for key, value in (
        ("distance_km", distance_km),
        ("duration_min", duration_min),
        ("pace", pace.strip() if pace else None),
        ("hr", hr.strip() if hr else None),
        ("ascent_m", ascent_m),
    ):
        if value is not None and value != "":
            target[key] = value
    return target


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["target"] = dict(out.get("target") or {})
    return out


def get(conn: psycopg.Connection[Any], workout_id: int) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {COLUMNS} FROM planned_workouts WHERE id = %s", (workout_id,)
    ).fetchone()
    if row is None:
        raise PlanError(f"there is no planned workout #{workout_id} (see coach plan show)")
    return _row(row)


def add(
    conn: psycopg.Connection[Any],
    *,
    day: date,
    title: str,
    kind: str | None = None,
    description: str | None = None,
    target: Mapping[str, Any] | None = None,
    status: str = "planned",
) -> dict[str, Any]:
    if not title.strip():
        raise PlanError("the title is empty")
    row = conn.execute(
        f"""INSERT INTO planned_workouts (date, title, kind, description, target, status)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}""",
        (
            day,
            title.strip(),
            check_kind(kind),
            description.strip() if description else None,
            Jsonb(dict(target)) if target else None,
            check_status(status),
        ),
    ).fetchone()
    assert row is not None
    return _row(row)


def change(
    conn: psycopg.Connection[Any],
    workout_id: int,
    *,
    day: date | None = None,
    title: str | None = None,
    kind: str | None = None,
    description: str | None = None,
    target: Mapping[str, Any] | None = None,
    clear_target: bool = False,
    status: str | None = None,
    activity_id: int | None = None,
    clear_activity: bool = False,
) -> dict[str, Any]:
    """Change the given fields. ``target`` keys are merged into the stored target (after
    clearing it with ``clear_target``)."""
    current = get(conn, workout_id)
    sets: dict[str, Any] = {}
    if day is not None:
        sets["date"] = day
    if title is not None:
        if not title.strip():
            raise PlanError("the title is empty")
        sets["title"] = title.strip()
    if kind is not None:
        sets["kind"] = check_kind(kind)
    if description is not None:
        sets["description"] = description.strip() or None
    if target or clear_target:
        merged = {} if clear_target else dict(current["target"])
        merged.update(target or {})
        sets["target"] = Jsonb(merged) if merged else None
    if status is not None:
        sets["status"] = check_status(status)
    if clear_activity:
        sets["activity_id"] = None
    elif activity_id is not None:
        _check_activity(conn, activity_id)
        sets["activity_id"] = activity_id
    if not sets:
        raise PlanError("nothing to change: give at least one option")
    assignments = ", ".join(f"{name} = %s" for name in sets)
    row = conn.execute(
        f"UPDATE planned_workouts SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
        (*sets.values(), workout_id),
    ).fetchone()
    assert row is not None
    return _row(row)


def move(
    conn: psycopg.Connection[Any], workout_id: int, new_day: date, today: date
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move a planned workout to ``new_day``. Returns ``(old, new)``.

    While its day lies in the future nothing has happened yet, so only the date changes
    (``old`` and ``new`` are the same row). Once its day has arrived, the row is kept with
    status ``moved`` (so the week shows what was planned there) and a copy is created on the
    new day; the two point at each other through ``target.moved_to`` / ``moved_from``.
    """
    current = get(conn, workout_id)
    if current["status"] != "planned":
        raise PlanError(
            f"#{workout_id} is {current['status']}; only planned workouts can be moved"
            " (correct a wrong date with coach plan change --date)"
        )
    if new_day == current["date"]:
        raise PlanError(f"#{workout_id} is already on {short(new_day)}")
    if current["date"] > today:
        moved = change(conn, workout_id, day=new_day)
        return moved, moved
    with conn.transaction():
        target = {**current["target"], "moved_from": workout_id}
        new = add(
            conn,
            day=new_day,
            title=current["title"],
            kind=current["kind"],
            description=current["description"],
            target=target,
        )
        old = change(conn, workout_id, status="moved", target={"moved_to": new["id"]})
    return old, new


def runs_on(conn: psycopg.Connection[Any], day: date) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """SELECT a.id, a.start_local, a.name, a.distance_m, a.duration_s, a.moving_s,
                      (SELECT array_agg(p.id ORDER BY p.id) FROM planned_workouts p
                        WHERE p.activity_id = a.id) AS linked_to
                 FROM activities a
                WHERE a.local_date = %s AND a.is_run
                ORDER BY a.start_utc""",
            (day,),
        ).fetchall()
    )


def _check_activity(conn: psycopg.Connection[Any], activity_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, local_date, is_run, type FROM activities WHERE id = %s", (activity_id,)
    ).fetchone()
    if row is None:
        raise PlanError(
            f"there is no activity {activity_id} in the database (not synced yet? see"
            " coach analysis list)"
        )
    return row


def mark(
    conn: psycopg.Connection[Any],
    workout_id: int,
    status: str,
    *,
    note: str | None = None,
    activity: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Mark a workout ``done`` or ``skipped``. For ``done``, ``activity`` is an activity id,
    ``auto`` (link the day's only run that is not linked yet) or ``none``. Returns the row and
    a remark for the agent (why nothing was linked)."""
    current = get(conn, workout_id)
    activity_id: int | None = None
    remark: str | None = None
    if status == "done" and activity not in (None, "none"):
        if activity == "auto":
            candidates = [r for r in runs_on(conn, current["date"]) if not r["linked_to"]]
            if len(candidates) == 1:
                activity_id = candidates[0]["id"]
            elif not candidates:
                remark = (
                    f"no unlinked run on {short(current['date'])} in the database, so nothing was"
                    " linked (not synced yet?)"
                )
            else:
                ids = ", ".join(str(r["id"]) for r in candidates)
                remark = (
                    f"several runs on {short(current['date'])} ({ids}); nothing was linked: pass"
                    " --activity ID"
                )
        else:
            try:
                activity_id = int(activity)
            except ValueError:
                raise PlanError(
                    f"--activity takes an activity id, auto or none, not {activity!r}"
                ) from None
    target = {"note": note.strip()} if note and note.strip() else None
    row = change(conn, workout_id, status=status, target=target, activity_id=activity_id)
    return row, remark


def link(conn: psycopg.Connection[Any], workout_id: int, activity_id: int) -> dict[str, Any]:
    """Link an activity; a workout that is still ``planned`` becomes ``done``."""
    current = get(conn, workout_id)
    status = "done" if current["status"] == "planned" else None
    return change(conn, workout_id, activity_id=activity_id, status=status)


def delete(conn: psycopg.Connection[Any], workout_id: int) -> dict[str, Any]:
    current = get(conn, workout_id)
    conn.execute("DELETE FROM planned_workouts WHERE id = %s", (workout_id,))
    return current


# The week view ---------------------------------------------------------------------------------


def week(conn: psycopg.Connection[Any], monday: date, *, weeks: int, today: date) -> dict[str, Any]:
    """Plan and activities from ``monday`` for ``weeks`` weeks, day by day, with totals."""
    last = monday + timedelta(days=7 * weeks - 1)
    workouts = [
        _row(r)
        for r in conn.execute(
            f"""SELECT {COLUMNS} FROM planned_workouts WHERE date BETWEEN %s AND %s
                ORDER BY date, id""",
            (monday, last),
        ).fetchall()
    ]
    activities = list(
        conn.execute(
            """SELECT id, local_date, start_local, type, is_run, name, distance_m,
                      coalesce(moving_s, duration_s) AS time_s, avg_hr, elev_gain_m
                 FROM activities WHERE local_date BETWEEN %s AND %s
                ORDER BY start_utc""",
            (monday, last),
        ).fetchall()
    )
    linked: dict[int, list[int]] = {}
    for w in workouts:
        if w["activity_id"]:
            linked.setdefault(w["activity_id"], []).append(w["id"])
    days = []
    for offset in range((last - monday).days + 1):
        day = monday + timedelta(days=offset)
        day_activities = []
        for a in activities:
            if a["local_date"] != day:
                continue
            item = dict(a)
            item["pace_s_per_km"] = (
                a["time_s"] / (a["distance_m"] / 1000)
                if a["is_run"] and a["distance_m"] and a["time_s"]
                else None
            )
            item["linked_to"] = linked.get(a["id"], [])
            day_activities.append(item)
        days.append(
            {
                "date": day,
                "workouts": [w for w in workouts if w["date"] == day],
                "activities": day_activities,
            }
        )
    return {
        "from": monday,
        "to": last,
        "today": today,
        "days": days,
        "totals": _totals(workouts, activities, today),
    }


def _totals(
    workouts: list[dict[str, Any]], activities: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    counted = [w for w in workouts if w["status"] != "moved"]
    runs = [a for a in activities if a["is_run"]]
    planned_km = sum(
        float(w["target"]["distance_km"])
        for w in counted
        if isinstance(w["target"].get("distance_km"), int | float)
    )
    return {
        "planned": len(counted),
        "done": sum(w["status"] == "done" for w in counted),
        "skipped": sum(w["status"] == "skipped" for w in counted),
        "moved": sum(w["status"] == "moved" for w in workouts),
        "open": sum(w["status"] == "planned" and w["date"] >= today for w in counted),
        "unmarked_past": sum(w["status"] == "planned" and w["date"] < today for w in counted),
        "planned_km": round(planned_km, 1),
        "runs": len(runs),
        "run_km": round(sum((a["distance_m"] or 0) for a in runs) / 1000, 1),
        "run_time_s": round(sum((a["time_s"] or 0) for a in runs)),
        "run_ascent_m": round(sum((a["elev_gain_m"] or 0) for a in runs)),
        "other_activities": len(activities) - len(runs),
    }


def target_text(target: Mapping[str, Any] | None) -> str:
    if not target:
        return ""
    parts = []
    if target.get("distance_km") is not None:
        parts.append(f"{float(target['distance_km']):g} km")
    if target.get("duration_min") is not None:
        parts.append(f"{float(target['duration_min']):g} min")
    if target.get("pace"):
        pace = str(target["pace"])
        bare = all(c.isdigit() or c in ":.-–— " for c in pace)
        parts.append(f"pace {pace}" + ("/km" if bare else ""))
    if target.get("hr"):
        parts.append(f"HR {target['hr']}")
    if target.get("ascent_m") is not None:
        parts.append(f"+{float(target['ascent_m']):g} m")
    skip = {*TARGET_KEYS, "note", "moved_from", "moved_to"}
    parts += [f"{k}: {v}" for k, v in target.items() if k not in skip]
    return " · ".join(parts)


def workout_line(w: Mapping[str, Any], today: date | None = None) -> str:
    kind = f"{w['kind']}: " if w.get("kind") else ""
    status = w["status"]
    if today is not None and status == "planned" and w["date"] < today:
        status = "planned, not marked"
    line = f"#{w['id']} [{status}] {kind}{w['title']}"
    target = target_text(w.get("target"))
    if target:
        line += f" · {target}"
    t = w.get("target") or {}
    if w.get("activity_id"):
        line += f" → activity {w['activity_id']}"
    if t.get("moved_to"):
        line += f" → moved to #{t['moved_to']}"
    if t.get("moved_from"):
        line += f" (moved here from #{t['moved_from']})"
    return line


def activity_line(a: Mapping[str, Any]) -> str:
    start = a["start_local"].strftime("%H:%M") if a.get("start_local") else "--:--"
    if a["is_run"]:
        bits = [
            f"{(a['distance_m'] or 0) / 1000:.1f} km",
            fmt.duration(a["time_s"]),
            f"{fmt.pace(a.get('pace_s_per_km'))}/km",
        ]
        if a.get("avg_hr"):
            bits.append(f"HR {round(a['avg_hr'])}")
        if a.get("elev_gain_m"):
            bits.append(f"+{round(a['elev_gain_m'])} m")
    else:
        bits = [a["type"], fmt.duration(a["time_s"])]
        if a.get("distance_m"):
            bits.append(f"{a['distance_m'] / 1000:.1f} km")
    line = f"{'ran' if a['is_run'] else 'other'} {a['id']} {start} " + " · ".join(bits)
    if a.get("name"):
        line += f' "{a["name"]}"'
    if a.get("linked_to"):
        line += " (linked to " + ", ".join(f"#{i}" for i in a["linked_to"]) + ")"
    return line


def week_text(view: Mapping[str, Any]) -> str:
    today: date = view["today"]
    t = view["totals"]
    lines = [f"Plan {short(view['from'])} to {short(view['to'])} (today {short(today)})"]
    summary = (
        f"Workouts: {t['planned']} planned · {t['done']} done · {t['skipped']} skipped ·"
        f" {t['open']} still to come"
    )
    if t["unmarked_past"]:
        summary += f" · {t['unmarked_past']} past and not marked"
    if t["moved"]:
        summary += f" · {t['moved']} moved"
    if t["planned_km"]:
        summary += f"; {t['planned_km']:g} km planned where a distance was given"
    lines.append(summary)
    ran = (
        f"Ran: {t['runs']} runs · {t['run_km']:g} km · {fmt.duration(t['run_time_s'])}"
        f" · +{t['run_ascent_m']} m"
    )
    if t["other_activities"]:
        ran += f" (+{t['other_activities']} other activities)"
    lines.append(ran)
    for d in view["days"]:
        day: date = d["date"]
        if day.weekday() == 0 and day != view["from"]:
            lines.append("")
        label = day.strftime("%a %m-%d")
        if day == today:
            label += " (today)"
        lines.append(label)
        for w in d["workouts"]:
            lines.append("  " + workout_line(w, today))
            if w.get("description"):
                lines.append("      " + " ".join(w["description"].split()))
            if (w.get("target") or {}).get("note"):
                lines.append("      note: " + w["target"]["note"])
        for a in d["activities"]:
            lines.append("  " + activity_line(a))
        if not d["workouts"] and not d["activities"]:
            lines.append("  -")
    return "\n".join(lines)


def changed_text(verb: str, row: Mapping[str, Any], today: date) -> str:
    return f"{verb} {workout_line(row)} on {short(row['date'])} ({relative(row['date'], today)})"
