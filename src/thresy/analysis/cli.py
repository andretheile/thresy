"""``coach analysis ...``: the typer plugin of the analysis package.

Heavy imports (pandas, numpy, matplotlib) happen inside the commands so ``coach --help`` stays
fast.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(
    help=(
        "Analyse runs, intervals, segments, courses, sleep and training volume.\n\n"
        "Choose an activity with the ACTIVITY argument (an id, 'last', or a date YYYY-MM-DD; a "
        "date picks that day's longest run) or with --last / --date. Default: the most recent "
        "run with per-second data. Use 'coach analysis list' to find ids.\n\n"
        "Every command prints text, or JSON with --json. --plot also writes a PNG under "
        "COACH_WORKSPACE_DIR/out/ and prints its path (labels in COACH_LANGUAGE). Times are "
        "timer times (watch pauses excluded) unless labelled elapsed; paces are min:s per km. "
        "Definitions of every number: docs/analysis.md."
    ),
    no_args_is_help=True,
)
segments_app = typer.Typer(
    help="Fixed segments (start and end point): list, import definitions, history, matches.",
    no_args_is_help=True,
)
route_app = typer.Typer(
    help="Courses: find runs on the same course; compare two runs aligned by position.",
    no_args_is_help=True,
)
app.add_typer(segments_app, name="segments")
app.add_typer(route_app, name="route")

ActivityArg = Annotated[
    str | None,
    typer.Argument(
        metavar="[ACTIVITY]",
        help="Activity id, 'last', or a date YYYY-MM-DD (default: the last run).",
        show_default=False,
    ),
]
LastOption = Annotated[bool, typer.Option("--last", help="The most recent run.")]
DateOption = Annotated[
    str | None, typer.Option("--date", help="The longest run on this local date (YYYY-MM-DD).")
]
PlotOption = Annotated[
    bool, typer.Option("--plot", help="Also write a PNG to COACH_WORKSPACE_DIR/out/.")
]
AllTypesOption = Annotated[
    bool, typer.Option("--all-types", help="Consider every activity type, not only runs.")
]


class Source(StrEnum):
    auto = "auto"
    laps = "laps"
    records = "records"


class Signal(StrEnum):
    speed = "speed"
    power = "power"


# Helpers --------------------------------------------------------------------------------------


@contextmanager
def _session(json_out: bool) -> Iterator[Any]:
    """A database connection; known errors become a clean ``error: ...`` exit."""
    from thresy import db
    from thresy.analysis.data import AnalysisError
    from thresy.config import ConfigError

    try:
        with db.connect() as conn:
            yield conn
    except (AnalysisError, ConfigError, ValueError, FileNotFoundError) as exc:
        raise fail(str(exc), as_json=json_out) from exc


def clean(value: Any) -> Any:
    """JSON-ready copy: numpy scalars to Python, private ``_keys`` and NaN dropped."""
    import math

    import numpy as np

    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _out(data: dict[str, Any], json_out: bool, text: Callable[[], str]) -> None:
    if json_out:
        emit(clean(data), as_json=True)
    else:
        emit(None, as_json=False, text=text())


def _settings() -> Any:
    from thresy.config import get_settings

    return get_settings()


def _today() -> date:
    return datetime.now(_settings().zone).date()


def _select(conn: Any, ref: str | None, last: bool, on_date: str | None, **kw: Any) -> Any:
    from thresy.analysis.data import resolve_activity

    return resolve_activity(conn, ref, last=last, on_date=on_date, **kw)


def _selected_line(sel: Any) -> str:
    line = f"{sel.activity.label}  [{sel.how}]"
    if sel.alternatives:
        others = ", ".join(
            f"{a.id} ({(a.distance_m or 0) / 1000:.1f} km)" for a in sel.alternatives
        )
        line += f"\nalso on that day: {others}"
    return line


def _plot_line(path: Path | None) -> str:
    return f"\nplot: {path}" if path else ""


def _band(day: dict[str, Any]) -> str:
    from thresy.analysis import fmt

    return f"{fmt.num(day['hrv_balanced_low'])}-{fmt.num(day['hrv_balanced_high'])}"


def _parse_date(value: str | None) -> date | None:
    from thresy.analysis.data import parse_date

    return parse_date(value) if value else None


# list -----------------------------------------------------------------------------------------


@app.command("list")
def list_cmd(
    since: Annotated[str | None, typer.Option(help="From this date (YYYY-MM-DD).")] = None,
    until: Annotated[str | None, typer.Option(help="Up to this date (YYYY-MM-DD).")] = None,
    days: Annotated[int | None, typer.Option(help="The last N days.")] = None,
    name: Annotated[str | None, typer.Option(help="Name contains this text.")] = None,
    activity_type: Annotated[
        str | None, typer.Option("--type", help="Garmin type, e.g. track_running.")
    ] = None,
    all_types: AllTypesOption = False,
    with_records: Annotated[
        bool, typer.Option("--with-records", help="Only activities with per-second data.")
    ] = False,
    limit: Annotated[int, typer.Option(help="At most this many (newest first).")] = 20,
    json_out: JsonOption = False,
) -> None:
    """Find activities: id, date, name, distance, time, pace, heart rate, ascent."""
    from thresy.analysis import fmt
    from thresy.analysis.data import find_activities

    with _session(json_out) as conn:
        start = _parse_date(since)
        if days:
            start = _today() - timedelta(days=days - 1)
        found = find_activities(
            conn,
            since=start,
            until=_parse_date(until),
            runs_only=not all_types and activity_type is None,
            with_records=with_records,
            types=[activity_type] if activity_type else None,
            name=name,
            limit=limit,
        )
    rows = [a.as_dict() for a in found]

    def text() -> str:
        table = [
            {
                "id": a.id,
                "date": fmt.when(a.start_local or a.local_date),
                "type": a.type,
                "name": (a.name or "")[:40],
                "km": fmt.km(a.distance_m),
                "time": fmt.duration(a.moving_s or a.duration_s),
                "pace": fmt.pace(
                    (a.moving_s or a.duration_s) / a.distance_m * 1000
                    if a.distance_m and (a.moving_s or a.duration_s)
                    else None
                ),
                "hr": fmt.num(a.avg_hr),
                "up": fmt.num(a.elev_gain_m),
                "fit": "yes" if a.has_records else "-",
            }
            for a in found
        ]
        cols = [
            ("id", "id"),
            ("date", "date"),
            ("type", "type"),
            ("name", "name"),
            ("km", "km"),
            ("time", "time"),
            ("pace", "pace"),
            ("hr", "HR"),
            ("up", "up m"),
            ("fit", "records"),
        ]
        return fmt.table(table, cols)

    _out({"activities": rows}, json_out, text)


# activity -------------------------------------------------------------------------------------


@app.command()
def activity(
    ref: ActivityArg = None,
    last: LastOption = False,
    on_date: DateOption = None,
    split_m: Annotated[float, typer.Option("--split-m", help="Split length in metres.")] = 1000,
    min_climb_m: Annotated[
        float, typer.Option("--min-climb-m", help="Smallest climb listed (net gain, m).")
    ] = 25,
    descents: Annotated[bool, typer.Option("--descents", help="List descents too.")] = False,
    all_types: AllTypesOption = False,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """One run in detail: splits, halves, heart-rate drift, climbs and stops."""
    from thresy.analysis import activity as act
    from thresy.analysis import fmt
    from thresy.analysis.data import load_records

    with _session(json_out) as conn:
        sel = _select(conn, ref, last, on_date, runs_only=not all_types)
        df = load_records(conn, sel.activity.id)
        result = act.summarize(
            sel.activity, df, split_m=split_m, climb_min_gain_m=min_climb_m, descents=descents
        )
    result["selected"] = sel.how
    path = None
    if plot:
        from thresy.analysis import plots

        path = plots.plot_activity(
            sel.activity.label,
            df,
            result["climbs"],
            lang=_settings().language,
            name=f"activity-{sel.activity.id}",
        )
        result["plot"] = str(path)

    def text() -> str:
        t = result["totals"] or {}
        lines = [_selected_line(sel)]
        if t:
            lines.append(
                f"distance {fmt.km(t['dist_m'])} km · timer {fmt.duration(t['time_s'])} · "
                f"elapsed {fmt.duration(t['elapsed_s'])} · moving {fmt.duration(t['moving_s'])} · "
                f"pace {fmt.pace(t['pace_s_per_km'])}/km"
            )
            lines.append(
                f"HR avg {fmt.num(t['avg_hr'])} max {fmt.num(t['max_hr'])} · ascent "
                f"{fmt.num(t['ascent_m'])} m (Garmin {fmt.num(t['garmin_elev_gain_m'])} m) · "
                f"descent {fmt.num(t['descent_m'])} m · cadence {fmt.num(t['cadence_spm'])} · "
                f"power {fmt.num(t['power_w'])} W · breathing {fmt.num(t['resp_rate'])}/min"
            )
        lines.append(f"\nsplits ({fmt.num(split_m)} m):")
        lines.append(
            fmt.table(
                [
                    {
                        "n": s["n"],
                        "km": fmt.km(s["end_m"], 2),
                        "time": fmt.duration(s["time_s"]),
                        "pace": fmt.pace(s["pace_s_per_km"]),
                        "hr": fmt.num(s["avg_hr"]),
                        "max": fmt.num(s["max_hr"]),
                        "up": fmt.num(s["ascent_m"]),
                        "down": fmt.num(s["descent_m"]),
                        "cad": fmt.num(s["cadence_spm"]),
                        "stop": fmt.duration(s["stopped_s"]) if s["stopped_s"] >= 5 else "",
                    }
                    for s in result["splits"]
                ],
                [
                    ("n", "#"),
                    ("km", "to km"),
                    ("time", "time"),
                    ("pace", "pace"),
                    ("hr", "HR"),
                    ("max", "max"),
                    ("up", "up"),
                    ("down", "down"),
                    ("cad", "cad"),
                    ("stop", "stopped"),
                ],
            )
        )
        h = result["halves"]
        if h:
            a, b = h["first"], h["second"]
            lines.append(
                f"\nhalves: first {fmt.pace(a['pace_s_per_km'])} HR {fmt.num(a['avg_hr'])} "
                f"(up {fmt.num(a['ascent_m'])} m) · second {fmt.pace(b['pace_s_per_km'])} "
                f"HR {fmt.num(b['avg_hr'])} (up {fmt.num(b['ascent_m'])} m) · pace change "
                f"{fmt.num(h['pace_diff_s_per_km'], 0)} s/km, HR change {fmt.num(h['hr_diff'], 0)}"
            )
        d = result["drift"]
        if "drift_bpm" in d:
            lines.append(
                f"drift (after {d['skip_s'] / 60:.0f} min, {fmt.duration(d['window_s'])}): HR "
                f"{d['first']['hr']} -> {d['second']['hr']} ({d['drift_bpm']:+} bpm), "
                f"decoupling {fmt.num(d['decoupling_pct'], 1)} % "
                f"(ascent {d['first']['ascent_m']:.0f} / {d['second']['ascent_m']:.0f} m)"
            )
        else:
            lines.append(f"drift: {d.get('note')}")
        lines.append(f"\nclimbs (>= {fmt.num(min_climb_m)} m):")
        lines.append(
            fmt.table(
                [
                    {
                        "n": c["n"],
                        "km": f"{c['start_km']:.2f}-{c['end_km']:.2f}",
                        "dist": fmt.num(c["dist_m"]),
                        "gain": f"{c['gain_m']:+.0f}",
                        "grade": f"{c['grade_pct']:.1f}%",
                        "time": fmt.duration(c["time_s"]),
                        "pace": fmt.pace(c["pace_s_per_km"]),
                        "vam": fmt.num(c["vam_m_per_h"]),
                        "hr": fmt.num(c["avg_hr"]),
                        "max": fmt.num(c["max_hr"]),
                    }
                    for c in result["climbs"]
                ],
                [
                    ("n", "#"),
                    ("km", "km"),
                    ("dist", "m"),
                    ("gain", "gain"),
                    ("grade", "grade"),
                    ("time", "time"),
                    ("pace", "pace"),
                    ("vam", "VAM"),
                    ("hr", "HR"),
                    ("max", "max"),
                ],
            )
        )
        st = result["stops"]
        lines.append(
            f"\nstopped: paused {fmt.duration(st['paused_s'])}, standing "
            f"{fmt.duration(st['standing_s'])}"
        )
        for s in st["stops"]:
            lines.append(
                f"  km {s['at_km']:.2f}  {fmt.duration(s['duration_s'])}  {s['kind']}"
                + (f"  {fmt.clock(s['start_local'])}" if s.get("start_local") else "")
            )
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


# intervals ------------------------------------------------------------------------------------


def _rep_table(reps: list[dict[str, Any]], *, with_date: bool = False) -> str:
    from thresy.analysis import fmt

    rows = [
        {
            "n": r["n"],
            "start": fmt.clock(r.get("start_local")),
            "time": fmt.duration(r["time_s"]),
            "dist": fmt.num(r["dist_m"]),
            "pace": fmt.pace(r["pace_s_per_km"]),
            "hr": fmt.num(r["avg_hr"]),
            "last30": fmt.num(r["hr_last30"]),
            "max": fmt.num(r["max_hr"]),
            "drop": fmt.num(r["hr_drop_60"]),
            "rec": fmt.duration(r["recovery_s"]),
            "cad": fmt.num(r["cadence_spm"]),
            "stride": fmt.num(r["stride_m"], 2),
            "resp": f"{fmt.num(r['resp_avg'])}/{fmt.num(r['resp_max'])}"
            if r.get("resp_avg") is not None
            else "-",
            "power": fmt.num(r["power_w"]),
        }
        for r in reps
    ]
    cols = [
        ("n", "#"),
        ("start", "start"),
        ("time", "time"),
        ("dist", "m"),
        ("pace", "pace"),
        ("hr", "HR"),
        ("last30", "HR30"),
        ("max", "max"),
        ("drop", "drop60"),
        ("rec", "rec"),
        ("cad", "cad"),
        ("stride", "stride"),
        ("resp", "breath"),
        ("power", "W"),
    ]
    return fmt.table(rows, cols)


@app.command()
def intervals(
    ref: ActivityArg = None,
    last: LastOption = False,
    on_date: DateOption = None,
    source: Annotated[
        Source,
        typer.Option(help="auto: laps from a workout or lap presses, else records; or force one."),
    ] = Source.auto,
    signal: Annotated[
        Signal,
        typer.Option(help="Signal for detection from records: speed, or power for hill repeats."),
    ] = Signal.speed,
    min_work_s: Annotated[
        float, typer.Option("--min-work-s", help="Shortest rep when detecting from records.")
    ] = 15,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """Reps of an interval session: pace, HR, recovery, cadence, stride, breathing, power.

    Reps come from workout laps or lap presses when they describe the session, otherwise from
    the per-second speed (--signal power for hill repeats). Per rep: time, distance, pace, HR
    average, over the last 30 s and maximum, HR drop in the first 60 s of recovery, recovery
    length, cadence, stride length, breathing rate and power.
    """
    from thresy.analysis import fmt
    from thresy.analysis import intervals as iv
    from thresy.analysis.data import load_laps, load_records

    with _session(json_out) as conn:
        sel = _select(conn, ref, last, on_date)
        df = load_records(conn, sel.activity.id)
        laps = load_laps(conn, sel.activity.id, df["ts"].iloc[0] if len(df) else None)
        result = iv.analyze(
            sel.activity,
            df,
            laps,
            source=source.value,
            min_work_s=min_work_s,
            signal=signal.value,
        )
    result["selected"] = sel.how
    path = None
    if plot and result["reps"]:
        from thresy.analysis import plots

        path = plots.plot_intervals(
            sel.activity.label,
            df,
            result["reps"],
            lang=_settings().language,
            name=f"intervals-{sel.activity.id}",
        )
        result["plot"] = str(path)

    def text() -> str:
        lines = [_selected_line(sel)]
        how = {
            "workout": "workout steps (lap intensity)",
            "laps": f"lap presses, split by {result['signal']}",
            "records": f"per-second {result['signal']}, threshold {result['threshold']}",
        }.get(result["source"])
        if not result["reps"]:
            lines.append(f"no intervals found ({result['note'] or result['source']})")
            return "\n".join(lines)
        lines.append(f"{len(result['reps'])} reps from {how}")
        lines.append(_rep_table(result["reps"]))
        s = result["summary"]
        lines.append(
            f"\nall reps: {fmt.km(s['dist_m'])} km in {fmt.duration(s['time_s'])}, pace "
            f"{fmt.pace(s['pace_s_per_km'])}, HR {fmt.num(s['avg_hr'])} (last 30 s "
            f"{fmt.num(s['hr_last30'])}, max {fmt.num(s['max_hr'])}), HR first->last rep "
            f"{fmt.num(s['hr_first_to_last'], 0)}, drop60 {fmt.num(s['hr_drop_60'])}, recovery "
            f"{fmt.duration(s['recovery_s'])} (median)"
        )
        if result["recoveries"]:
            lines.append("recoveries:")
            lines.append(
                fmt.table(
                    [
                        {
                            "after": r["after_rep"],
                            "time": fmt.duration(r["time_s"]),
                            "dist": fmt.num(r["dist_m"]),
                            "pace": fmt.pace(r["pace_s_per_km"]),
                            "hr": fmt.num(r["avg_hr"]),
                            "min": fmt.num(r["min_hr"]),
                        }
                        for r in result["recoveries"]
                    ],
                    [
                        ("after", "after #"),
                        ("time", "time"),
                        ("dist", "m"),
                        ("pace", "pace"),
                        ("hr", "HR"),
                        ("min", "min HR"),
                    ],
                )
            )
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


@app.command("intervals-compare")
def intervals_compare(
    rep_m: Annotated[
        float | None, typer.Option("--rep-m", help="Rep distance in metres, e.g. 1000.")
    ] = None,
    rep_s: Annotated[
        float | None, typer.Option("--rep-s", help="Rep duration in seconds, e.g. 180.")
    ] = None,
    tolerance: Annotated[
        float, typer.Option(help="Allowed deviation from the rep length, as a fraction.")
    ] = 0.15,
    since: Annotated[str | None, typer.Option(help="From this date (YYYY-MM-DD).")] = None,
    until: Annotated[str | None, typer.Option(help="Up to this date (YYYY-MM-DD).")] = None,
    min_reps: Annotated[int, typer.Option("--min-reps", help="Fewest matching reps.")] = 3,
    first: Annotated[
        int | None,
        typer.Option("--first", help="Average only the first N matching reps of each session."),
    ] = None,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """Compare interval sessions of the same kind over time, e.g. all ~1 km reps.

    Scans every run with per-second data (optionally in a date range) and lists the sessions
    with enough reps of the given distance or duration: pace, HR, HR over the last 30 s, HR
    drop, recovery, cadence and stride, averaged over the matching reps (or the first N).
    """
    from thresy.analysis import fmt
    from thresy.analysis import intervals as iv

    if rep_m is None and rep_s is None:
        raise fail("give --rep-m or --rep-s", as_json=json_out, code=2)

    def progress(message: str) -> None:
        if not json_out:
            typer.echo(message, err=True)

    with _session(json_out) as conn:
        result = iv.compare_sessions(
            conn,
            rep_m=rep_m,
            rep_s=rep_s,
            tolerance=tolerance,
            since=_parse_date(since),
            until=_parse_date(until),
            min_reps=min_reps,
            first_n=first,
            progress=progress,
        )
    kind = f"{fmt.num(rep_m)} m" if rep_m else f"{fmt.num(rep_s)} s"
    path = None
    if plot and result["sessions"]:
        from thresy.analysis import plots

        path = plots.plot_sessions(
            f"{kind} reps",
            result["sessions"],
            lang=_settings().language,
            name=f"intervals-compare-{kind.replace(' ', '')}",
        )
        result["plot"] = str(path)

    def text() -> str:
        lines = [
            f"sessions with >= {min_reps} reps of {kind} (±{tolerance:.0%}); analysed "
            f"{result['analysed']} of {result['scanned']} runs with per-second data"
            + (f"; first {first} reps averaged" if first else "")
        ]
        lines.append(
            fmt.table(
                [
                    {
                        "date": s["date"].isoformat(),
                        "id": s["activity_id"],
                        "name": (s["name"] or "")[:28],
                        "reps": f"{s['n_used']}/{s['n_reps']}",
                        "pace": fmt.pace(s["pace_s_per_km"]),
                        "hr": fmt.num(s["avg_hr"]),
                        "last30": fmt.num(s["hr_last30"]),
                        "max": fmt.num(s["max_hr"]),
                        "drop": fmt.num(s["hr_drop_60"]),
                        "rec": fmt.duration(s["recovery_s"]),
                        "cad": fmt.num(s["cadence_spm"]),
                        "stride": fmt.num(s["stride_m"], 2),
                        "src": s["source"],
                    }
                    for s in result["sessions"]
                ],
                [
                    ("date", "date"),
                    ("id", "id"),
                    ("name", "name"),
                    ("reps", "reps"),
                    ("pace", "pace"),
                    ("hr", "HR"),
                    ("last30", "HR30"),
                    ("max", "max"),
                    ("drop", "drop60"),
                    ("rec", "rec"),
                    ("cad", "cad"),
                    ("stride", "stride"),
                    ("src", "from"),
                ],
            )
        )
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


# sensor ---------------------------------------------------------------------------------------


@app.command()
def sensor(
    ref: ActivityArg = None,
    last: LastOption = False,
    on_date: DateOption = None,
    all_types: AllTypesOption = False,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """Can the heart rate be trusted? Cadence lock, jumps, spikes, dropouts.

    Cadence lock = the wrist sensor reports the step rate as heart rate. Also gives the average
    and maximum heart rate without the flagged samples, and a verdict (ok, suspect, unreliable).
    """
    from thresy.analysis import fmt
    from thresy.analysis import sensor as sn
    from thresy.analysis.data import load_records

    with _session(json_out) as conn:
        sel = _select(conn, ref, last, on_date, runs_only=not all_types)
        df = load_records(conn, sel.activity.id, ["dist", "speed", "hr", "cad", "power"])
        result = sn.check(df, sel.activity)
    result["selected"] = sel.how
    path = None
    if plot:
        from thresy.analysis import plots

        path = plots.plot_sensor(
            sel.activity.label,
            df,
            result,
            lang=_settings().language,
            name=f"sensor-{sel.activity.id}",
        )
        result["plot"] = str(path)

    def text() -> str:
        lines = [_selected_line(sel)]
        if not result["has_hr"]:
            return "\n".join([*lines, "no heart rate recorded"])
        lines.append(
            f"verdict: {result['verdict']} · flagged {fmt.duration(result['flagged_s'])} "
            f"({result['flagged_share']:.0%} of moving time) · HR avg {fmt.num(result['avg_hr'])} "
            f"max {fmt.num(result['max_hr'])} · without flagged samples avg "
            f"{fmt.num(result['avg_hr_clean'])} max {fmt.num(result['max_hr_clean'])}"
        )
        for x in result["cadence_lock"]:
            lines.append(
                f"cadence lock ({x['confidence']}): km {fmt.num(x['from_km'], 2)}-"
                f"{fmt.num(x['to_km'], 2)}, {fmt.duration(x['duration_s'])}, HR {x['avg_hr']} vs "
                f"cadence {x['avg_cadence']} (mean diff {x['mean_abs_diff']}, r {x['corr']}, "
                f"edge jump {x['edge_jump_bpm']} bpm)"
            )
        for x in result["spikes"]:
            lines.append(
                f"spike: km {fmt.num(x['at_km'], 2)}, {x['duration_s']} s up to {x['max_hr']:.0f}"
                f" (around {x['baseline_hr']})"
            )
        if result["jumps"]:
            lines.append(f"jumps of >= 20 bpm within 3 s: {len(result['jumps'])}")
        for x in result["dropouts"]:
            lines.append(f"dropout: {fmt.duration(x['duration_s'])} without heart rate")
        for x in result["level_shifts"]:
            lines.append(
                f"level shift at km {fmt.num(x['at_km'], 2)}: "
                f"HR {x['hr_before']} -> {x['hr_after']}"
                f" at the same {x['effort']} (change {x['effort_change']:.0%}); one side is wrong."
                f" Share within 5 bpm of cadence: before {x['near_cadence_before']}, after"
                f" {x['near_cadence_after']} (near 1 = sensor reading the step rate)"
            )

        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


# route ----------------------------------------------------------------------------------------


@route_app.command("find")
def route_find(
    ref: ActivityArg = None,
    last: LastOption = False,
    on_date: DateOption = None,
    min_coverage: Annotated[
        float, typer.Option("--min-coverage", help="Share of the course the other run covers.")
    ] = 0.8,
    limit: Annotated[int, typer.Option(help="At most this many.")] = 10,
    all_types: AllTypesOption = False,
    json_out: JsonOption = False,
) -> None:
    """Other runs on the same course as ACTIVITY, for 'route compare'.

    coverage = share of ACTIVITY's track the other run covers; on_course = share of the other
    run that lies on ACTIVITY's track.
    """
    from thresy.analysis import fmt
    from thresy.analysis import route as rt

    with _session(json_out) as conn:
        sel = _select(conn, ref, last, on_date, runs_only=not all_types)
        found = rt.find_same_course(
            conn, sel.activity, min_coverage=min_coverage, limit=limit, all_types=all_types
        )
    result = {"reference": sel.activity.as_dict(), "selected": sel.how, "matches": found}

    def text() -> str:
        return (
            _selected_line(sel)
            + "\n"
            + fmt.table(
                [
                    {
                        "date": m["date"].isoformat(),
                        "id": m["activity_id"],
                        "name": (m["name"] or "")[:40],
                        "km": fmt.num(m["distance_km"], 2),
                        "cov": f"{m['coverage']:.0%}",
                        "on": f"{m['on_course']:.0%}",
                    }
                    for m in found
                ],
                [
                    ("date", "date"),
                    ("id", "id"),
                    ("name", "name"),
                    ("km", "km"),
                    ("cov", "coverage"),
                    ("on", "on course"),
                ],
            )
        )

    _out(result, json_out, text)


@route_app.command("compare")
def route_compare(
    a: Annotated[str, typer.Argument(help="Reference run (the course): id, date or 'last'.")],
    b: Annotated[str, typer.Argument(help="Run compared with it: id, date or 'last'.")],
    step_m: Annotated[float, typer.Option("--step-m", help="Section length in metres.")] = 1000,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """Two runs on the same course aligned by position: where time was won or lost.

    A is the course (the reference). Total and per-section time difference B minus A
    (positive = B slower), heart rate of both, the course's climbs, and time stopped.
    """
    from thresy.analysis import fmt
    from thresy.analysis import route as rt

    with _session(json_out) as conn:
        sa = _select(conn, a, False, None, runs_only=False)
        sb = _select(conn, b, False, None, runs_only=False)
        result = rt.compare(conn, sa.activity, sb.activity, step_m=step_m)
    path = None
    if plot:
        from thresy.analysis import plots

        path = plots.plot_route(
            f"{sa.activity.local_date} vs {sb.activity.local_date}",
            result,
            str(sa.activity.local_date),
            str(sb.activity.local_date),
            lang=_settings().language,
            name=f"route-{sa.activity.id}-{sb.activity.id}",
        )
        result["plot"] = str(path)

    def text() -> str:
        r = result
        lines = [
            f"A: {sa.activity.label}",
            f"B: {sb.activity.label}",
            f"common course km {r['common']['from_km']:.2f}-{r['common']['to_km']:.2f} "
            f"(course {fmt.km(r['course_length_m'])} km; {r['on_route_b']:.0%} of B on it)",
            f"A {fmt.duration(r['time_a_s'])} HR {fmt.num(r['hr_a'])} stopped "
            f"{fmt.duration(r['stopped_a_s'])} · B {fmt.duration(r['time_b_s'])} HR "
            f"{fmt.num(r['hr_b'])} stopped {fmt.duration(r['stopped_b_s'])} · B - A "
            f"{fmt.signed_duration(r['delta_s'])}",
            "",
            fmt.table(
                [
                    {
                        "n": s["n"],
                        "km": f"{s['from_km']:.1f}-{s['to_km']:.1f}",
                        "up": fmt.num(s["gain_m"]),
                        "a": fmt.duration(s["time_a_s"]),
                        "b": fmt.duration(s["time_b_s"]),
                        "d": fmt.signed_duration(s["delta_s"]),
                        "cum": fmt.signed_duration(s["cum_delta_s"]),
                        "hra": fmt.num(s["hr_a"]),
                        "hrb": fmt.num(s["hr_b"]),
                    }
                    for s in r["sections"]
                ],
                [
                    ("n", "#"),
                    ("km", "km"),
                    ("up", "up m"),
                    ("a", "A"),
                    ("b", "B"),
                    ("d", "B-A"),
                    ("cum", "cum"),
                    ("hra", "HR A"),
                    ("hrb", "HR B"),
                ],
            ),
        ]
        if r["climbs"]:
            lines.append("\nclimbs on the course:")
            lines.append(
                fmt.table(
                    [
                        {
                            "km": f"{c['from_km']:.1f}-{c['to_km']:.1f}",
                            "gain": f"+{c['gain_m']}",
                            "grade": f"{c['grade_pct']}%",
                            "a": fmt.duration(c["time_a_s"]),
                            "b": fmt.duration(c["time_b_s"]),
                            "d": fmt.signed_duration(c["delta_s"]),
                            "hra": fmt.num(c["hr_a"]),
                            "hrb": fmt.num(c["hr_b"]),
                        }
                        for c in r["climbs"]
                    ],
                    [
                        ("km", "km"),
                        ("gain", "gain"),
                        ("grade", "grade"),
                        ("a", "A"),
                        ("b", "B"),
                        ("d", "B-A"),
                        ("hra", "HR A"),
                        ("hrb", "HR B"),
                    ],
                )
            )
        if r["most_lost"]:
            lines.append(f"most time lost (B) in sections: {', '.join(map(str, r['most_lost']))}")
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


# segments -------------------------------------------------------------------------------------


@segments_app.command("list")
def segments_list(json_out: JsonOption = False) -> None:
    """Defined segments and their number of cached efforts."""
    from thresy.analysis import fmt
    from thresy.analysis import segments as sg

    with _session(json_out) as conn:
        segs = sg.list_segments(conn)
        counts = {
            r["segment_key"]: r["n"]
            for r in conn.execute(
                "SELECT segment_key, count(*) AS n FROM segment_efforts GROUP BY 1"
            ).fetchall()
        }
    data = [{**s.as_dict(), "cached_efforts": counts.get(s.key, 0)} for s in segs]

    def text() -> str:
        if not segs:
            return "no segments defined (add some with: coach analysis segments import FILE)"
        return fmt.table(
            [
                {
                    "key": s.key,
                    "name": s.name,
                    "dist": f"{fmt.num(s.dist_min_m)}-{fmt.num(s.dist_max_m)}",
                    "tol": fmt.num(s.tol_m),
                    "up": "yes" if s.uphill else "no",
                    "n": counts.get(s.key, 0),
                }
                for s in segs
            ],
            [
                ("key", "key"),
                ("name", "name"),
                ("dist", "dist m"),
                ("tol", "tol m"),
                ("up", "uphill"),
                ("n", "cached"),
            ],
        )

    _out({"segments": data}, json_out, text)


@segments_app.command("import")
def segments_import(
    file: Annotated[Path, typer.Argument(help="TOML or JSON file with segment definitions.")],
    json_out: JsonOption = False,
) -> None:
    """Add or update segment definitions from a TOML or JSON file.

    The format is in docs/analysis.md. A changed definition clears that segment's cached
    efforts.
    """
    from thresy.analysis import segments as sg

    with _session(json_out) as conn:
        segs = sg.load_segment_file(file)
        counts = sg.upsert_segments(conn, segs)
    data = {"file": str(file), "segments": [s.key for s in segs], **counts}
    _out(
        data,
        json_out,
        lambda: (
            f"{len(segs)} segment(s): new {counts['new']}, updated {counts['updated']}, "
            f"unchanged {counts['unchanged']}"
        ),
    )


@segments_app.command("history")
def segments_history(
    key: Annotated[str, typer.Argument(help="Segment key (see 'segments list').")],
    since: Annotated[str | None, typer.Option(help="Only efforts from this date on.")] = None,
    at_hr: Annotated[
        float | None,
        typer.Option("--at-hr", help="Heart rate for the pace-at-HR column (default: median)."),
    ] = None,
    hr_band: Annotated[
        str | None,
        typer.Option("--hr-band", help="Only efforts with average HR in this range, e.g. 145-155."),
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Recompute all efforts and rewrite the cache.")
    ] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Compute without reading or writing the cache.")
    ] = False,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """Every effort on a segment over time, best effort, and pace at a common heart rate.

    Per effort: date, time, distance, gain, pace, HR (avg, max, last 60 s), metres per heartbeat
    and pace adjusted to a common heart rate. Efforts are cached in segment_efforts; on a
    read-only connection they are computed without the cache.
    """
    from thresy.analysis import fmt
    from thresy.analysis import segments as sg

    band = None
    if hr_band:
        try:
            lo, hi = (float(x) for x in hr_band.split("-"))
            band = (lo, hi)
        except ValueError as exc:
            raise fail("--hr-band must look like 145-155", as_json=json_out, code=2) from exc
    with _session(json_out) as conn:
        seg = sg.get_segment(conn, key)
        if seg is None:
            raise fail(
                f"unknown segment {key!r} (see: coach analysis segments list)", as_json=json_out
            )
        found = sg.segment_efforts(conn, seg, refresh=refresh, use_cache=not no_cache)
        efforts = found["efforts"]
        result = sg.history(conn, seg, efforts, at_hr=at_hr, hr_band=band)
    start = _parse_date(since)
    if start:
        result["efforts"] = [
            e for e in result["efforts"] if e["start_local"] and e["start_local"].date() >= start
        ]
        result["count"] = len(result["efforts"])
        result["best"] = min(result["efforts"], key=lambda e: e["duration_s"], default=None)
    result["cache"] = {k: v for k, v in found.items() if k != "efforts"}
    path = None
    if plot and result["efforts"]:
        from thresy.analysis import plots

        path = plots.plot_segment(
            seg.name, result["efforts"], lang=_settings().language, name=f"segment-{seg.key}"
        )
        result["plot"] = str(path)

    def text() -> str:
        r = result
        lines = [
            f"{seg.name} ({seg.key}): {r['count']} efforts in {r['activities']} activities"
            + (f", HR band {hr_band}" if band else "")
            + f" · cache {r['cache']['cache']} ({r['cache']['scanned']} scanned)"
        ]
        if r["best"]:
            b = r["best"]
            lines.append(
                f"best {fmt.duration(b['duration_s'])} on {fmt.when(b['start_local'])} "
                f"(pace {fmt.pace(b['pace_s_per_km'])}, HR {fmt.num(b['avg_hr'])})"
            )
        if r["fit"]:
            lines.append(
                f"pace at HR {fmt.num(r['at_hr'])}: fit over {r['fit']['n']} efforts, "
                f"{r['fit']['slope_s_per_bpm']} s/km per bpm, r² {r['fit']['r2']}"
            )
        lines.append(
            fmt.table(
                [
                    {
                        "date": fmt.when(e["start_local"]),
                        "id": e["activity_id"],
                        "time": fmt.duration(e["duration_s"]),
                        "dist": fmt.num(e["dist_m"]),
                        "gain": fmt.num(e["gain_m"]),
                        "pace": fmt.pace(e["pace_s_per_km"]),
                        "hr": fmt.num(e["avg_hr"]),
                        "max": fmt.num(e["max_hr"]),
                        "last60": fmt.num(e["hr_last60"]),
                        "mpb": fmt.num(e["m_per_beat"], 3),
                        "adj": fmt.pace(e["pace_at_hr"]),
                    }
                    for e in r["efforts"]
                ],
                [
                    ("date", "date"),
                    ("id", "id"),
                    ("time", "time"),
                    ("dist", "m"),
                    ("gain", "gain"),
                    ("pace", "pace"),
                    ("hr", "HR"),
                    ("max", "max"),
                    ("last60", "HR60"),
                    ("mpb", "m/beat"),
                    ("adj", "pace@HR"),
                ],
            )
        )
        if r["per_year"]:
            lines.append("per year:")
            lines.append(
                fmt.table(
                    [
                        {
                            "year": y["year"],
                            "n": y["efforts"],
                            "best": fmt.duration(y["best_s"]),
                            "median": fmt.duration(y["median_s"]),
                            "hr": fmt.num(y["avg_hr"]),
                            "adj": fmt.pace(y["median_pace_at_hr"]),
                        }
                        for y in r["per_year"]
                    ],
                    [
                        ("year", "year"),
                        ("n", "efforts"),
                        ("best", "best"),
                        ("median", "median"),
                        ("hr", "avg HR"),
                        ("adj", "pace@HR"),
                    ],
                )
            )
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


@segments_app.command("match")
def segments_match(
    ref: ActivityArg = None,
    last: LastOption = False,
    on_date: DateOption = None,
    json_out: JsonOption = False,
) -> None:
    """Efforts of one activity on every defined segment."""
    from thresy.analysis import fmt
    from thresy.analysis import segments as sg

    with _session(json_out) as conn:
        sel = _select(conn, ref, last, on_date, runs_only=False)
        found = sg.match_activity(conn, sel.activity, sg.list_segments(conn))
    for e in found:
        e["start_local"] = sel.activity.local(e["start_utc"])
    result = {"activity": sel.activity.as_dict(), "selected": sel.how, "efforts": found}

    def text() -> str:
        return (
            _selected_line(sel)
            + "\n"
            + fmt.table(
                [
                    {
                        "seg": e["segment_key"],
                        "start": fmt.clock(e["start_local"]),
                        "time": fmt.duration(e["duration_s"]),
                        "dist": fmt.num(e["dist_m"]),
                        "gain": fmt.num(e["gain_m"]),
                        "pace": fmt.pace(e["pace_s_per_km"]),
                        "hr": fmt.num(e["avg_hr"]),
                        "last60": fmt.num(e["hr_last60"]),
                    }
                    for e in found
                ],
                [
                    ("seg", "segment"),
                    ("start", "start"),
                    ("time", "time"),
                    ("dist", "m"),
                    ("gain", "gain"),
                    ("pace", "pace"),
                    ("hr", "HR"),
                    ("last60", "HR60"),
                ],
            )
        )

    _out(result, json_out, text)


# night, wellness, weeks -----------------------------------------------------------------------


@app.command()
def night(
    wake_date: Annotated[
        str | None,
        typer.Option("--date", help="Wake date YYYY-MM-DD: the night that ended that morning."),
    ] = None,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """One night's sleep stages, awake periods and HRV readings (default: last night).

    The night is keyed by wake date: --date 2030-01-02 is the night that ended that morning.
    HRV: average overall, while asleep (awake periods excluded), over the first 3 h of sleep,
    minimum and maximum with times, and per stage.
    """
    from thresy.analysis import fmt
    from thresy.analysis import night as nt

    settings = _settings()
    day = _parse_date(wake_date) or _today()
    with _session(json_out) as conn:
        result = nt.night(conn, day, settings.zone)
    path = None
    if plot and result.get("readings"):
        from thresy.analysis import plots

        path = plots.plot_night(
            f"{day.isoformat()}", result, lang=settings.language, name=f"night-{day.isoformat()}"
        )
        result["plot"] = str(path)

    def text() -> str:
        r = result
        if "hrv" not in r:
            latest = r.get("latest_synced")
            hint = f"; latest synced night: {latest} (use --date {latest})" if latest else ""
            return f"night with wake date {day}: not synced yet{hint}"
        s, h, g = r["sleep"], r["hrv"], r["garmin_hrv"]
        lines = [
            f"night with wake date {day}: {fmt.clock(s['start_local'])}-{fmt.clock(s['end_local'])}"
            f" · sleep {fmt.duration(s['sleep_s'])} · score {fmt.num(s['score'])} · deep "
            f"{fmt.duration(s['deep_s'])}, light {fmt.duration(s['light_s'])}, REM "
            f"{fmt.duration(s['rem_s'])}, awake {fmt.duration(s['awake_s'])}",
            f"Garmin HRV {fmt.num(g['night_avg'])} ms ({g['status'] or '-'}, 7-day "
            f"{fmt.num(g['weekly_avg'])}, balanced {fmt.num(g['balanced_low'])}-"
            f"{fmt.num(g['balanced_high'])}) · resting HR {fmt.num(r['resting_hr'])}",
            f"HRV readings: {h['n']} · avg all {fmt.num(h['avg_all'], 1)} · asleep (without "
            f"{h['n_awake']} awake) {fmt.num(h['avg_asleep'], 1)} · first 3 h "
            f"{fmt.num(h['avg_first_3h'], 1)} ({h['n_first_3h']} readings)",
        ]
        if h["min"]:
            lines.append(
                f"min {h['min']['value_ms']:.0f} at {fmt.clock(h['min']['local'])} · max "
                f"{h['max']['value_ms']:.0f} at {fmt.clock(h['max']['local'])} · per stage: "
                + ", ".join(f"{k} {fmt.num(v, 1)}" for k, v in h["per_stage"].items())
            )
        if r["awake_periods"]:
            lines.append(
                "awake: "
                + ", ".join(
                    f"{fmt.clock(a['start_local'])}-{fmt.clock(a['end_local'])} "
                    f"({fmt.duration(a['duration_s'])})"
                    for a in r["awake_periods"]
                )
            )
        else:
            lines.append("awake: none recorded")
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


@app.command()
def wellness(
    days: Annotated[int, typer.Option(help="Number of days (wake dates).")] = 28,
    end: Annotated[str | None, typer.Option(help="Last wake date (default: today).")] = None,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    """HRV against its balanced range, resting HR, sleep duration and score over N days.

    Also compares the last 7 days with the 7 before and counts nights below the range.
    """
    from thresy.analysis import fmt
    from thresy.analysis import wellness as wl

    last = _parse_date(end) or _today()
    with _session(json_out) as conn:
        result = wl.trend(conn, last, days)
    path = None
    if plot:
        from thresy.analysis import plots

        path = plots.plot_wellness(
            f"{result['from']} – {result['to']}",
            result["daily"],
            lang=_settings().language,
            name=f"wellness-{last.isoformat()}",
        )
        result["plot"] = str(path)

    def text() -> str:
        s = result["summary"]

        def blk(b: dict[str, Any]) -> str:
            return (
                f"HRV {fmt.num(b['hrv'], 1)}, resting HR {fmt.num(b['resting_hr'], 1)}, sleep "
                f"{fmt.duration(b['sleep_s'])}, score {fmt.num(b['sleep_score'])} "
                f"({b['nights']} nights)"
            )

        lines = [
            f"wake dates {result['from']} to {result['to']}",
            f"last 7 days: {blk(s['last7'])}",
            f"7 days before: {blk(s['prev7'])}",
            f"nights below balanced range: {s['nights_below_range']}, above: "
            f"{s['nights_above_range']}, without sleep data: {s['days_without_sleep']}",
            fmt.table(
                [
                    {
                        "date": d["wake_date"].isoformat(),
                        "hrv": fmt.num(d["hrv_night_avg"]),
                        "week": fmt.num(d["hrv_weekly_avg"]),
                        "range": _band(d) if d["hrv_balanced_low"] is not None else "-",
                        "flag": "below"
                        if d["below_range"]
                        else ("above" if d["above_range"] else ""),
                        "status": d["hrv_status"] or "-",
                        "rhr": fmt.num(d["resting_hr"]),
                        "sleep": fmt.duration(d["sleep_s"]),
                        "score": fmt.num(d["sleep_score"]),
                        "steps": fmt.num(d["steps"]),
                    }
                    for d in result["daily"]
                ],
                [
                    ("date", "wake date"),
                    ("hrv", "HRV"),
                    ("week", "7d"),
                    ("range", "balanced"),
                    ("flag", ""),
                    ("status", "status"),
                    ("rhr", "RHR"),
                    ("sleep", "sleep"),
                    ("score", "score"),
                    ("steps", "steps"),
                ],
            ),
        ]
        return "\n".join(lines) + _plot_line(path)

    _out(result, json_out, text)


@app.command()
def weeks(
    n_weeks: Annotated[int, typer.Option("--weeks", help="Number of weeks.")] = 12,
    all_types: AllTypesOption = False,
    json_out: JsonOption = False,
) -> None:
    """Weekly distance, time, ascent, runs and longest run; year-to-date totals.

    Weeks run Monday to Sunday; the year to date is compared with the same period last year.
    """
    from thresy.analysis import fmt
    from thresy.analysis import weeks as wk

    with _session(json_out) as conn:
        result = wk.weeks(conn, _today(), n_weeks, runs_only=not all_types)

    def text() -> str:
        def row(w: dict[str, Any], label: str) -> dict[str, Any]:
            return {
                "label": label,
                "runs": w["runs"],
                "km": fmt.num(w["distance_km"], 1),
                "time": fmt.duration(w["time_s"]),
                "up": fmt.num(w["ascent_m"]),
                "long": fmt.num(w["longest_km"], 1),
            }

        rows = [row(w, f"{w['from']} (W{w['week']:02d})") for w in result["weeks"]]
        rows += [
            row(result["ytd"], f"{result['today'].year} to date"),
            row(result["ytd_last_year"], f"{result['today'].year - 1} same period"),
        ]
        return fmt.table(
            rows,
            [
                ("label", "week from"),
                ("runs", "runs"),
                ("km", "km"),
                ("time", "time"),
                ("up", "up m"),
                ("long", "longest km"),
            ],
        )

    _out(result, json_out, text)
