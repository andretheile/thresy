"""``coach races``: the athlete's race list in ``races``.

The agent keeps it with its own database role (``coach_agent`` may INSERT, UPDATE and DELETE
``races``). ``result`` is a JSON object: ``time`` (``H:MM:SS``) and ``time_s`` with ``source``
(``official`` when the athlete or the results list gave it, ``watch`` when it comes from the
linked activity), ``place``, ``note``, and ``watch`` (distance, time, heart rate and ascent of
the linked activity).

Status: ``planned`` (intends to race), ``maybe`` (undecided), ``done``, ``dnf`` (started, did
not finish), ``dns`` (did not start: decided against it, ill, or the event was cancelled).
Priority: ``A`` (the goal race), ``B`` (important, but trained through), ``C`` (training race).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, time, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from thresy.analysis import fmt
from thresy.tools.dates import relative, short

STATUSES = ("planned", "maybe", "done", "dnf", "dns")
TIME_SOURCES = ("official", "reported")
PRIORITIES = ("A", "B", "C")
COLUMNS = (
    "id, date, name, distance_km, elev_m, location, start_time_local, priority, goal, status,"
    " activity_id, result, url, notes"
)
_TIME = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")


class RaceError(ValueError):
    """The request cannot be carried out; the message says what to do instead."""


def check_status(status: str) -> str:
    value = status.strip().lower()
    if value not in STATUSES:
        raise RaceError(f"unknown status {status!r}; use one of: {', '.join(STATUSES)}")
    return value


def check_priority(priority: str | None) -> str | None:
    if priority is None:
        return None
    value = priority.strip().upper()
    if value not in PRIORITIES:
        raise RaceError(f"unknown priority {priority!r}; use A, B or C")
    return value


def parse_start(text: str | None) -> time | None:
    if text is None:
        return None
    try:
        return time.fromisoformat(text.strip())
    except ValueError:
        raise RaceError(f"not a start time: {text!r} (use HH:MM)") from None


def parse_duration(text: str) -> float:
    """``1:31:07`` / ``42:10`` / ``18:42.4`` -> seconds."""
    match = _TIME.match(text.strip())
    if not match:
        raise RaceError(f"not a finishing time: {text!r} (use H:MM:SS or MM:SS)")
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["result"] = dict(out["result"]) if out.get("result") else None
    return out


def get(conn: psycopg.Connection[Any], race_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT {COLUMNS} FROM races WHERE id = %s", (race_id,)).fetchone()
    if row is None:
        raise RaceError(f"there is no race #{race_id} (see coach races list --all)")
    return _row(row)


def add(
    conn: psycopg.Connection[Any],
    *,
    day: date,
    name: str,
    distance_km: float | None = None,
    elev_m: float | None = None,
    location: str | None = None,
    start: time | None = None,
    priority: str | None = None,
    goal: str | None = None,
    status: str = "planned",
    url: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise RaceError("the race name is empty")
    duplicate = conn.execute(
        "SELECT id FROM races WHERE date = %s AND lower(name) = lower(%s)", (day, name.strip())
    ).fetchone()
    if duplicate:
        raise RaceError(
            f"{name.strip()!r} on {day.isoformat()} is already race #{duplicate['id']};"
            " use coach races change"
        )
    row = conn.execute(
        f"""INSERT INTO races (date, name, distance_km, elev_m, location, start_time_local,
                               priority, goal, status, url, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}""",
        (
            day,
            name.strip(),
            distance_km,
            elev_m,
            location,
            start,
            check_priority(priority),
            goal,
            check_status(status),
            url,
            notes,
        ),
    ).fetchone()
    assert row is not None
    return _row(row)


FIELDS = (
    "date",
    "name",
    "distance_km",
    "elev_m",
    "location",
    "start_time_local",
    "priority",
    "goal",
    "status",
    "url",
    "notes",
    "activity_id",
    "result",
)


class _Clear:
    def __repr__(self) -> str:
        return "CLEAR"


CLEAR: Any = _Clear()
"""Value for :func:`change` that empties a column."""


def change(
    conn: psycopg.Connection[Any],
    race_id: int,
    values: Mapping[str, Any],
    *,
    add_note: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Set the given columns (``None`` values are skipped, :data:`CLEAR` empties a column).
    ``add_note`` appends a line dated ``today`` to the notes."""
    current = get(conn, race_id)
    sets: dict[str, Any] = {}
    for name, value in values.items():
        if name not in FIELDS:
            raise RaceError(f"unknown field {name!r}")
        if value is None:
            continue
        if value is CLEAR:
            sets[name] = None
        elif name == "status":
            sets[name] = check_status(value)
        elif name == "priority":
            sets[name] = check_priority(value)
        elif name == "result":
            sets[name] = Jsonb(dict(value))
        elif name == "name" and not str(value).strip():
            raise RaceError("the race name is empty")
        else:
            sets[name] = value
    if add_note and add_note.strip():
        stamp = (today or date.today()).isoformat()
        line = f"{stamp}: {add_note.strip()}"
        notes = sets.get("notes", current["notes"])
        sets["notes"] = f"{notes}\n{line}" if notes else line
    if not sets:
        raise RaceError("nothing to change: give at least one option")
    assignments = ", ".join(f"{name} = %s" for name in sets)
    row = conn.execute(
        f"UPDATE races SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
        (*sets.values(), race_id),
    ).fetchone()
    assert row is not None
    return _row(row)


def _activity(conn: psycopg.Connection[Any], activity_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT id, local_date, name, distance_m,
                  -- timer time: closest to a race time (moving time drops the stops)
                  coalesce(duration_s, elapsed_s, moving_s) AS time_s,
                  moving_s, elapsed_s, avg_hr, elev_gain_m
             FROM activities WHERE id = %s""",
        (activity_id,),
    ).fetchone()
    if row is None:
        raise RaceError(
            f"there is no activity {activity_id} in the database (not synced yet? see"
            " coach analysis list)"
        )
    return row


def record_result(
    conn: psycopg.Connection[Any],
    race_id: int,
    *,
    finish_time: str | None = None,
    activity: str | None = "auto",
    place: str | None = None,
    note: str | None = None,
    status: str = "done",
    source: str = "official",
) -> tuple[dict[str, Any], str | None]:
    """Record how the race went. ``activity`` is an id, ``auto`` (the longest run on the race
    date) or ``none``. ``source`` labels ``finish_time``: ``official`` (results list) or
    ``reported`` (the athlete's word, not checked). Returns the row and a remark."""
    race = get(conn, race_id)
    remark = None
    linked = None
    if activity not in (None, "none"):
        if activity == "auto":
            row = conn.execute(
                """SELECT id FROM activities WHERE local_date = %s AND is_run
                    ORDER BY distance_m DESC NULLS LAST LIMIT 1""",
                (race["date"],),
            ).fetchone()
            if row:
                linked = _activity(conn, row["id"])
            else:
                remark = f"no run on {short(race['date'])} in the database, so none was linked"
        else:
            try:
                linked = _activity(conn, int(activity))
            except ValueError:
                raise RaceError(
                    f"--activity takes an activity id, auto or none, not {activity!r}"
                ) from None
    result: dict[str, Any] = dict(race["result"] or {})
    if finish_time:
        seconds = parse_duration(finish_time)
        if source not in TIME_SOURCES:
            raise RaceError(f"unknown source {source!r}; use official or reported")
        result.update(time_s=seconds, time=fmt.duration(seconds), source=source)
    if linked is not None:
        result["watch"] = {
            "activity_id": linked["id"],
            "distance_km": round((linked["distance_m"] or 0) / 1000, 2),
            "time_s": linked["time_s"],
            "moving_s": linked["moving_s"],
            "elapsed_s": linked["elapsed_s"],
            "avg_hr": linked["avg_hr"],
            "elev_gain_m": linked["elev_gain_m"],
        }
        if "time_s" not in result and linked["time_s"]:
            result.update(
                time_s=linked["time_s"], time=fmt.duration(linked["time_s"]), source="watch"
            )
    if place:
        result["place"] = place
    if note:
        result["note"] = note
    values: dict[str, Any] = {"status": status, "result": result}
    if linked is not None:
        values["activity_id"] = linked["id"]
    return change(conn, race_id, values), remark


def delete(conn: psycopg.Connection[Any], race_id: int) -> dict[str, Any]:
    current = get(conn, race_id)
    conn.execute("DELETE FROM races WHERE id = %s", (race_id,))
    return current


def listing(
    conn: psycopg.Connection[Any], *, today: date, which: str, past_days: int = 60
) -> list[dict[str, Any]]:
    """``which``: ``upcoming`` (today on), ``past``, ``all``, or ``default`` (upcoming plus
    the past ``past_days`` days)."""
    if which == "upcoming":
        where, params = "date >= %s", (today,)
    elif which == "past":
        where, params = "date < %s", (today,)
    elif which == "all":
        where, params = "true", ()
    else:
        where, params = "date >= %s", (today - timedelta(days=past_days),)
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM races WHERE {where} ORDER BY date, id", params
    ).fetchall()
    return [_row(r) for r in rows]


# Text -------------------------------------------------------------------------------------------


def race_line(r: Mapping[str, Any], today: date) -> str:
    bits = [f"#{r['id']}", short(r["date"]), f"({relative(r['date'], today)})"]
    if r.get("priority"):
        bits.append(r["priority"])
    bits.append(r["name"])
    details = []
    if r.get("distance_km") is not None:
        details.append(f"{r['distance_km']:g} km")
    if r.get("elev_m") is not None:
        details.append(f"+{r['elev_m']:g} m")
    if r.get("start_time_local"):
        details.append(f"start {r['start_time_local'].strftime('%H:%M')}")
    if r.get("location"):
        details.append(str(r["location"]))
    line = " ".join(bits)
    if details:
        line += " · " + " · ".join(details)
    line += f" [{r['status']}]"
    if r.get("goal"):
        line += f" goal: {r['goal']}"
    result = r.get("result") or {}
    if result.get("time"):
        line += f" result: {result['time']} ({result.get('source', '?')})"
    if result.get("place"):
        line += f", {result['place']}"
    if r.get("activity_id"):
        line += f" → activity {r['activity_id']}"
    return line


def detail_text(r: Mapping[str, Any], today: date) -> str:
    lines = [race_line(r, today)]
    if r.get("url"):
        lines.append(f"url: {r['url']}")
    result = r.get("result") or {}
    watch = result.get("watch")
    if watch:
        lines.append(
            f"watch: {watch.get('distance_km')} km · {fmt.duration(watch.get('time_s'))}"
            f" · HR {fmt.num(watch.get('avg_hr'))} · +{fmt.num(watch.get('elev_gain_m'))} m"
            f" (activity {watch.get('activity_id')})"
        )
    if result.get("note"):
        lines.append(f"result note: {result['note']}")
    if r.get("notes"):
        lines.append("notes:")
        lines += ["  " + line for line in str(r["notes"]).splitlines()]
    return "\n".join(lines)


def listing_text(rows: list[dict[str, Any]], today: date) -> str:
    if not rows:
        return "no races in this range (add one with coach races add)"
    upcoming = [r for r in rows if r["date"] >= today]
    past = [r for r in rows if r["date"] < today]
    lines: list[str] = []
    if upcoming:
        lines.append("Upcoming")
        lines += ["  " + race_line(r, today) for r in upcoming]
    if past:
        if lines:
            lines.append("")
        lines.append("Past")
        lines += ["  " + race_line(r, today) for r in past]
    return "\n".join(lines)
