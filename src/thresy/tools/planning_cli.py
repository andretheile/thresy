"""``coach weather``, ``coach plan …`` and ``coach races …``: the coach's planning tools.

Mounted at the top level of ``coach`` through :mod:`thresy.tools.cli`. The help texts are
written for the agent, which learns the commands from ``--help``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any

import typer

from thresy.cli import JsonOption, emit, fail
from thresy.tools.dates import DAY_HELP


def _help(*paragraphs: str) -> str:
    """One line per paragraph, so the terminal wraps it cleanly. No square brackets: the help
    renderer would read them as markup."""
    return "\n\n".join(" ".join(p.split()) for p in paragraphs)


app = typer.Typer()


@contextmanager
def _db(json_out: bool, what: str) -> Iterator[Any]:
    """A connection with the caller's role; known errors become a clean ``error: …`` exit."""
    import psycopg

    from thresy import db
    from thresy.config import ConfigError
    from thresy.tools.plan import PlanError
    from thresy.tools.races import RaceError

    try:
        with db.connect() as conn:
            yield conn
    except (PlanError, RaceError, ConfigError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as exc:
        message = f"this database role may not change {what} (permission denied)"
        raise fail(message, as_json=json_out) from exc
    except psycopg.Error as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise fail(f"database error: {detail}", as_json=json_out) from exc


def _today() -> date:
    from thresy.tools.dates import local_today

    return local_today()


def _day(text: str, today: date, json_out: bool) -> date:
    from thresy.tools.dates import parse_day

    try:
        return parse_day(text, today)
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out) from exc


# coach weather ---------------------------------------------------------------------------------

WEATHER_HELP = _help(
    "Weather forecast for planning runs: daily for the next days, hourly for chosen days.",
    """Without a place it uses COACH_HOME_LOCATION, where the athlete usually runs. A place is a
    name (Springfield), a name with region or country to pick the right one (Springfield,
    Illinois), or coordinates as lat,lon. The output names the place it found and other places
    with the same name: check that it is the right one, especially for race locations.""",
    """Default: daily values for 7 days (weather, min/max temperature, rain amount, chance and
    hours, wind and gusts, daylight) and hourly values from now to 22:00 today and 6:00-22:00
    tomorrow (temperature, feels-like, rain, chance of rain, wind). --date picks other days,
    also past ones (up to 92 days back, e.g. the weather during a run). Times are local to the
    place. Look before you plan or move a session outdoors, and say what the weather means for
    the run (a slippery track, heat, wind on an exposed course), not just the numbers.""",
    """Each day first gets a summary line in words, built from the numbers of its daily row.
    The weather word is the worst weather of the day, not all-day weather. Rain (or snow):
    "dry" below 0.2 mm with a chance below 30 %, "rain possible" otherwise below 0.2 mm, "a
    little rain" from 0.2 mm, "rain" from 1 mm, "heavy rain" from 10 mm. Wind, from the
    higher-ranking of gusts and mean wind: "little wind", "breezy" (gusts from 30 or wind from
    20 km/h), "windy" (50 or 40), "stormy" (75 or 60). "frost" at 0 °C or below, "warm" from
    25 °C, "hot" from 30 °C. Call a day dry only if its line says "dry", and calm only if it
    says "little wind".""",
    "Source: Open-Meteo (open-meteo.com), no key needed. Needs internet access.",
)


@app.command("weather", help=WEATHER_HELP)
def weather_command(
    place: Annotated[
        str | None,
        typer.Argument(
            help="Place name or lat,lon (default: COACH_HOME_LOCATION).", show_default=False
        ),
    ] = None,
    country: Annotated[
        str | None, typer.Option("--country", help="Two-letter country code to narrow the search.")
    ] = None,
    days: Annotated[
        int, typer.Option("--days", min=1, max=16, help="Days of the daily overview.")
    ] = 7,
    dates: Annotated[
        list[str] | None,
        typer.Option("--date", help=f"Hourly values for this day ({DAY_HELP}); repeatable."),
    ] = None,
    hours: Annotated[
        str, typer.Option("--hours", help="Hours shown in the hourly tables, e.g. 5-21.")
    ] = "6-22",
    daily_only: Annotated[
        bool, typer.Option("--daily-only", help="Only the daily overview.")
    ] = False,
    json_out: JsonOption = False,
) -> None:
    import logging
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from thresy.tools import weather

    logging.getLogger("httpx").setLevel(logging.WARNING)  # no request lines in the output
    from thresy.tools.dates import parse_day

    settings = weather.WeatherSettings()
    where = place or settings.home_location
    if not where:
        raise fail(
            "no place given and COACH_HOME_LOCATION is not set: pass a place name or lat,lon",
            as_json=json_out,
        )
    today = _today()
    try:
        first, _, last = hours.partition("-")
        window = (int(first), int(last or first))
        if not (0 <= window[0] <= window[1] <= 23):
            raise ValueError
    except ValueError:
        raise fail(f"--hours takes a range such as 6-22, not {hours!r}", as_json=json_out) from None
    explicit = bool(dates)
    try:
        hourly_days = [parse_day(d, today) for d in dates or []]
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    if not explicit and not daily_only:
        hourly_days = [today, today + timedelta(days=1)]
    if daily_only:
        hourly_days = []
    ahead = max([days, *((d - today).days + 1 for d in hourly_days if d >= today)])
    behind = max([0, *((today - d).days for d in hourly_days if d < today)])
    try:
        with weather.make_client() as client:
            found = weather.resolve_place(client, where, country=country)
            forecast = weather.fetch_forecast(client, found, forecast_days=ahead, past_days=behind)
    except weather.WeatherError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    try:
        now = datetime.now(ZoneInfo(forecast.timezone)).replace(tzinfo=None)
    except (ZoneInfoNotFoundError, ValueError):
        now = datetime.now()
    daily = weather.daily_rows(forecast, today, today + timedelta(days=days - 1))
    hourly = [
        (
            d,
            weather.hourly_rows(
                forecast, d, window, not_before=now if (d == today and not explicit) else None
            ),
        )
        for d in hourly_days
    ]
    data = {
        "place": forecast.place,
        "timezone": forecast.timezone,
        "elevation_m": forecast.elevation_m,
        "daily": daily,
        "hourly": {d.isoformat(): rows for d, rows in hourly},
        "source": "open-meteo.com",
    }
    emit(data, as_json=json_out, text=weather.format_text(forecast, daily, hourly))


# coach plan ------------------------------------------------------------------------------------

PLAN_HELP = _help(
    "The training plan (table planned_workouts), with what was actually run next to it.",
    """Start with coach plan show: this week, Monday to Sunday, every planned workout and every
    activity of each day, plus totals. Write a workout down as soon as it is agreed with the
    athlete, and change it in the same run when the athlete changes it, so the plan stays the
    record of what was agreed (sessions end every day; the plan does not). Mark workouts done
    (linking the run) or skipped when you debrief or brief.""",
    f"""Days: {DAY_HELP}. They are the athlete's local dates (COACH_TZ). Ids are the #numbers in
    the output. Text by default, --json for everything.""",
)
plan_app = typer.Typer(help=PLAN_HELP, invoke_without_command=True)
app.add_typer(plan_app, name="plan")

TargetDistance = Annotated[
    float | None, typer.Option("--distance-km", help="Target distance in km.")
]
TargetDuration = Annotated[
    float | None, typer.Option("--duration-min", help="Target duration in minutes.")
]
TargetPace = Annotated[
    str | None, typer.Option("--pace", help="Target pace per km as text, e.g. 4:10-4:15.")
]
TargetHr = Annotated[str | None, typer.Option("--hr", help="Heart-rate target as text, e.g. <150.")]
TargetAscent = Annotated[float | None, typer.Option("--ascent-m", help="Planned ascent in m.")]
KindOption = Annotated[
    str | None,
    typer.Option(
        "--kind",
        help="easy, recovery, long, tempo, threshold, intervals, hills, fartlek, strides, race,"
        " cross, strength, rest or other.",
    ),
]
DescriptionOption = Annotated[
    str | None,
    typer.Option(
        "--description",
        "-d",
        help="The session as the athlete should run it: warm-up, reps, recoveries, cues.",
    ),
]


def _target(
    distance_km: float | None,
    duration_min: float | None,
    pace: str | None,
    hr: str | None,
    ascent_m: float | None,
    target_json: str | None,
    json_out: bool,
) -> dict[str, Any]:
    import json

    from thresy.tools.plan import build_target

    extra: dict[str, Any] = {}
    if target_json:
        try:
            extra = json.loads(target_json)
        except ValueError as exc:
            raise fail(f"--target-json is not valid JSON: {exc}", as_json=json_out) from exc
        if not isinstance(extra, dict):
            raise fail("--target-json must be a JSON object", as_json=json_out)
    return build_target(
        distance_km=distance_km,
        duration_min=duration_min,
        pace=pace,
        hr=hr,
        ascent_m=ascent_m,
        extra=extra,
    )


@plan_app.callback()
def plan_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        plan_show(week=None, weeks=1, json_out=False)


@plan_app.command(
    "show",
    help=_help(
        "Show a week of the plan, day by day, with the activities of each day next to it.",
        """WEEK is this (default), next, last, or any day inside the wanted week. --weeks 2
        shows two weeks from there. Workouts whose day has passed but are still 'planned' are
        flagged 'not marked': mark them done or skipped. Ran lines show the activity id to
        link.""",
    ),
)
def plan_show(
    week: Annotated[
        str | None, typer.Argument(help="this, next, last, or a day of the week.")
    ] = None,
    weeks: Annotated[int, typer.Option("--weeks", min=1, max=12, help="Number of weeks.")] = 1,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan
    from thresy.tools.dates import parse_week

    today = _today()
    try:
        monday = parse_week(week, today)
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    with _db(json_out, "the plan") as conn:
        view = plan.week(conn, monday, weeks=weeks, today=today)
    emit(view, as_json=json_out, text=plan.week_text(view))


@plan_app.command(
    "add",
    help=_help(
        "Add a planned workout.",
        """Example: coach plan add fri "5x1 km threshold" --kind threshold --distance-km 12
        --pace 4:10-4:15 -d "15 min easy, 5x1 km with 2 min jog, 10 min easy". Put the session
        itself in the title or description in the athlete's words and the numbers into the
        target options; --target-json adds further keys.""",
    ),
)
def plan_add(
    day: Annotated[str, typer.Argument(help=DAY_HELP)],
    title: Annotated[str, typer.Argument(help="Short title, e.g. Long run, hilly.")],
    kind: KindOption = None,
    description: DescriptionOption = None,
    distance_km: TargetDistance = None,
    duration_min: TargetDuration = None,
    pace: TargetPace = None,
    hr: TargetHr = None,
    ascent_m: TargetAscent = None,
    target_json: Annotated[
        str | None, typer.Option("--target-json", help="Further target keys as a JSON object.")
    ] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    when = _day(day, today, json_out)
    target = _target(distance_km, duration_min, pace, hr, ascent_m, target_json, json_out)
    with _db(json_out, "the plan") as conn:
        row = plan.add(
            conn, day=when, title=title, kind=kind, description=description, target=target
        )
    emit(row, as_json=json_out, text=plan.changed_text("added", row, today))


@plan_app.command(
    "change",
    help=_help(
        "Change a planned workout: any of its fields.",
        """Target options are merged into the stored target (--clear-target empties it first).
        --date corrects the date in place; to postpone a session use coach plan move, which
        keeps the history. --status sets planned, done, skipped or moved directly; --activity
        links an activity id, --activity none removes the link.""",
    ),
)
def plan_change(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    day: Annotated[str | None, typer.Option("--date", help=DAY_HELP)] = None,
    title: Annotated[str | None, typer.Option("--title", help="New title.")] = None,
    kind: KindOption = None,
    description: DescriptionOption = None,
    distance_km: TargetDistance = None,
    duration_min: TargetDuration = None,
    pace: TargetPace = None,
    hr: TargetHr = None,
    ascent_m: TargetAscent = None,
    target_json: Annotated[
        str | None, typer.Option("--target-json", help="Further target keys as a JSON object.")
    ] = None,
    clear_target: Annotated[
        bool, typer.Option("--clear-target", help="Empty the target before merging.")
    ] = False,
    status: Annotated[
        str | None, typer.Option("--status", help="planned, done, skipped or moved.")
    ] = None,
    activity: Annotated[
        str | None, typer.Option("--activity", help="Activity id to link, or none.")
    ] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    when = _day(day, today, json_out) if day else None
    target = _target(distance_km, duration_min, pace, hr, ascent_m, target_json, json_out)
    activity_id = None
    if activity is not None and activity.lower() != "none":
        try:
            activity_id = int(activity)
        except ValueError:
            raise fail(
                f"--activity takes an id or none, not {activity!r}", as_json=json_out
            ) from None
    with _db(json_out, "the plan") as conn:
        row = plan.change(
            conn,
            workout_id,
            day=when,
            title=title,
            kind=kind,
            description=description,
            target=target or None,
            clear_target=clear_target,
            status=status,
            activity_id=activity_id,
            clear_activity=activity is not None and activity.lower() == "none",
        )
    emit(row, as_json=json_out, text=plan.changed_text("changed", row, today))


@plan_app.command(
    "move",
    help=_help(
        "Move a planned workout to another day.",
        """While its day is still in the future only the date changes. Once its day has come,
        the original stays in the plan as 'moved' (so the week shows what was planned there)
        and a copy is created on the new day; the output names the new id.""",
    ),
)
def plan_move(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    day: Annotated[str, typer.Argument(help=DAY_HELP)],
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    when = _day(day, today, json_out)
    with _db(json_out, "the plan") as conn:
        old, new = plan.move(conn, workout_id, when, today)
    if old["id"] == new["id"]:
        text = plan.changed_text("moved", new, today)
    else:
        text = f"#{old['id']} marked moved; " + plan.changed_text("now", new, today)
    emit({"old": old, "new": new}, as_json=json_out, text=text)


@plan_app.command(
    "done",
    help=_help(
        "Mark a planned workout done and link the run that fulfilled it.",
        """--activity auto (default) links the only run of that day that is not linked yet;
        with several runs that day it links nothing and lists them, so pass --activity ID.
        --note keeps a short line on how it went (e.g. 'felt easy, last rep the fastest').""",
    ),
)
def plan_done(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    activity: Annotated[
        str, typer.Option("--activity", help="Activity id, auto or none.")
    ] = "auto",
    note: Annotated[str | None, typer.Option("--note", help="How it went, one line.")] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    with _db(json_out, "the plan") as conn:
        row, remark = plan.mark(conn, workout_id, "done", note=note, activity=activity)
    text = plan.changed_text("done:", row, today) + (f"\nnote: {remark}" if remark else "")
    emit({"workout": row, "remark": remark}, as_json=json_out, text=text)


@plan_app.command(
    "skip",
    help=_help(
        "Mark a planned workout skipped.",
        "--note says why (ill, bad night, no childcare …), so later weeks can be read right.",
    ),
)
def plan_skip(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    note: Annotated[str | None, typer.Option("--note", help="Why, one line.")] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    with _db(json_out, "the plan") as conn:
        row, _ = plan.mark(conn, workout_id, "skipped", note=note)
    emit(row, as_json=json_out, text=plan.changed_text("skipped:", row, today))


@plan_app.command(
    "link",
    help="Link an activity to a planned workout; a workout still 'planned' becomes 'done'.",
)
def plan_link(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    activity_id: Annotated[int, typer.Argument(help="The activity id (see coach plan show).")],
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    with _db(json_out, "the plan") as conn:
        row = plan.link(conn, workout_id, activity_id)
    emit(row, as_json=json_out, text=plan.changed_text("linked", row, today))


@plan_app.command(
    "delete",
    help="Delete a planned workout that should not have been entered (not for skipped ones).",
)
def plan_delete(
    workout_id: Annotated[int, typer.Argument(help="The workout's id (#number).")],
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import plan

    today = _today()
    with _db(json_out, "the plan") as conn:
        row = plan.delete(conn, workout_id)
    emit(row, as_json=json_out, text=plan.changed_text("deleted", row, today))


# coach races -----------------------------------------------------------------------------------

RACES_HELP = _help(
    "The athlete's races (table races): upcoming and past, with goals and results.",
    """Keep every race the athlete mentions here, including the ones he is undecided about
    (status maybe), with date, distance, ascent, start time and place checked against the
    organiser's page where you can. Priority A is the goal race, B important, C a training
    race. Status: planned, maybe, done, dnf, dns. Record the result after the race with
    coach races result.""",
    f"Days: {DAY_HELP}. Text by default, --json for everything.",
)
races_app = typer.Typer(help=RACES_HELP, no_args_is_help=True)
app.add_typer(races_app, name="races")

RaceDistance = Annotated[float | None, typer.Option("--distance-km", help="Distance in km.")]
RaceElev = Annotated[float | None, typer.Option("--elev-m", help="Total ascent in m.")]
RaceLocation = Annotated[str | None, typer.Option("--location", help="Town or venue.")]
RaceStart = Annotated[str | None, typer.Option("--start", help="Local start time HH:MM.")]
RacePriority = Annotated[str | None, typer.Option("--priority", help="A, B or C.")]
RaceGoal = Annotated[
    str | None, typer.Option("--goal", help="The goal, e.g. 'sub 1:45' or 'finish relaxed'.")
]
RaceUrl = Annotated[str | None, typer.Option("--url", help="Organiser's page.")]


@races_app.command(
    "list",
    help="List races: upcoming ones and those of the last 60 days (or --upcoming, --past, --all).",
)
def races_list(
    upcoming: Annotated[bool, typer.Option("--upcoming", help="Only today and later.")] = False,
    past: Annotated[bool, typer.Option("--past", help="Only past races.")] = False,
    all_: Annotated[bool, typer.Option("--all", help="Every race.")] = False,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    which = "all" if all_ else "upcoming" if upcoming else "past" if past else "default"
    today = _today()
    with _db(json_out, "races") as conn:
        rows = races.listing(conn, today=today, which=which)
    emit(rows, as_json=json_out, text=races.listing_text(rows, today))


@races_app.command("show", help="One race with all its fields, notes and result.")
def races_show(
    race_id: Annotated[int, typer.Argument(help="The race's id (#number).")],
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    today = _today()
    with _db(json_out, "races") as conn:
        row = races.get(conn, race_id)
    emit(row, as_json=json_out, text=races.detail_text(row, today))


@races_app.command(
    "add",
    help=_help(
        "Add a race.",
        """Example: coach races add 2030-04-14 "City Half Marathon" --distance-km 21.1
        --elev-m 120 --start 09:30 --location Springfield --priority A --goal "sub 1:30".
        Use --status maybe while the athlete has not decided. A race with the same date and
        name is refused (change it instead).""",
    ),
)
def races_add(
    day: Annotated[str, typer.Argument(help=DAY_HELP)],
    name: Annotated[str, typer.Argument(help="The race's name.")],
    distance_km: RaceDistance = None,
    elev_m: RaceElev = None,
    location: RaceLocation = None,
    start: RaceStart = None,
    priority: RacePriority = None,
    goal: RaceGoal = None,
    status: Annotated[str, typer.Option("--status", help="planned or maybe.")] = "planned",
    url: RaceUrl = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Anything else.")] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    today = _today()
    when = _day(day, today, json_out)
    with _db(json_out, "races") as conn:
        row = races.add(
            conn,
            day=when,
            name=name,
            distance_km=distance_km,
            elev_m=elev_m,
            location=location,
            start=races.parse_start(start),
            priority=priority,
            goal=goal,
            status=status,
            url=url,
            notes=notes,
        )
    emit(row, as_json=json_out, text="added " + races.race_line(row, today))


@races_app.command(
    "change",
    help=_help(
        "Change a race: any of its fields.",
        """--add-note appends a dated line to the notes (decisions, why the priority changed);
        --notes replaces them. --status dns when the athlete decides not to start.""",
    ),
)
def races_change(
    race_id: Annotated[int, typer.Argument(help="The race's id (#number).")],
    day: Annotated[str | None, typer.Option("--date", help=DAY_HELP)] = None,
    name: Annotated[str | None, typer.Option("--name", help="New name.")] = None,
    distance_km: RaceDistance = None,
    elev_m: RaceElev = None,
    location: RaceLocation = None,
    start: RaceStart = None,
    priority: RacePriority = None,
    goal: RaceGoal = None,
    status: Annotated[
        str | None, typer.Option("--status", help="planned, maybe, done, dnf or dns.")
    ] = None,
    url: RaceUrl = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Replace the notes.")] = None,
    add_note: Annotated[
        str | None, typer.Option("--add-note", help="Append a dated line to the notes.")
    ] = None,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    today = _today()
    try:
        start_time = races.parse_start(start) if start else None
    except races.RaceError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    values = {
        "date": _day(day, today, json_out) if day else None,
        "name": name,
        "distance_km": distance_km,
        "elev_m": elev_m,
        "location": location,
        "start_time_local": start_time,
        "priority": priority,
        "goal": goal,
        "status": status,
        "url": url,
        "notes": notes,
    }
    with _db(json_out, "races") as conn:
        row = races.change(conn, race_id, values, add_note=add_note, today=today)
    emit(row, as_json=json_out, text="changed " + races.race_line(row, today))


@races_app.command(
    "result",
    help=_help(
        "Record how a race went: finishing time, the activity, placing, a note.",
        """--time is the finishing time (H:MM:SS or MM:SS), with --source official when it comes
        from the results list and reported when the athlete just said it. Without --time the
        linked activity's timer time is stored, labelled watch. --activity auto (default) links
        the longest run on the race date. --status dnf or dns for a race that was not finished
        or not started.""",
    ),
)
def races_result(
    race_id: Annotated[int, typer.Argument(help="The race's id (#number).")],
    finish_time: Annotated[str | None, typer.Option("--time", help="Finishing time.")] = None,
    source: Annotated[
        str,
        typer.Option("--source", help="Where --time comes from: official (results) or reported."),
    ] = "official",
    activity: Annotated[
        str, typer.Option("--activity", help="Activity id, auto or none.")
    ] = "auto",
    place: Annotated[
        str | None, typer.Option("--place", help="Placing, e.g. '12th of 480, 3rd M35'.")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="How it went, one line.")] = None,
    status: Annotated[str, typer.Option("--status", help="done, dnf or dns.")] = "done",
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    today = _today()
    with _db(json_out, "races") as conn:
        row, remark = races.record_result(
            conn,
            race_id,
            finish_time=finish_time,
            source=source,
            activity=activity,
            place=place,
            note=note,
            status=status,
        )
    text = (
        "result recorded: " + races.race_line(row, today) + (f"\nnote: {remark}" if remark else "")
    )
    emit({"race": row, "remark": remark}, as_json=json_out, text=text)


@races_app.command("delete", help="Delete a race entered by mistake (use status dns otherwise).")
def races_delete(
    race_id: Annotated[int, typer.Argument(help="The race's id (#number).")],
    json_out: JsonOption = False,
) -> None:
    from thresy.tools import races

    today = _today()
    with _db(json_out, "races") as conn:
        row = races.delete(conn, race_id)
    emit(row, as_json=json_out, text="deleted " + races.race_line(row, today))
