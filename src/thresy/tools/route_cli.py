"""``coach route plan|show|export``: running routes as GPX files (docs/routes.md).

Mounted at the top level of ``coach`` through :mod:`thresy.tools.cli`. The help texts are
written for the agent, which learns the commands from ``--help``. Heavy imports (numpy,
matplotlib, gpxpy) happen inside the commands.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from thresy.cli import JsonOption, emit, fail


def _help(*paragraphs: str) -> str:
    """One line per paragraph, so the terminal wraps it cleanly. No square brackets: the help
    renderer would read them as markup."""
    return "\n\n".join(" ".join(p.split()) for p in paragraphs)


ROUTE_HELP = _help(
    "Running routes as GPX files: plan new ones, read one the athlete sent, export a past run.",
    """coach route plan asks a routing service (openrouteservice, on OpenStreetMap data) for
    candidate routes and writes each as a GPX file to out/. coach route show reads a GPX file,
    for example one the athlete planned in komoot and sent (under inbox/). coach route export
    writes a past activity as a GPX route. The athlete imports the files into komoot or Garmin
    Connect themselves; this tool writes to no account, and nothing goes into the database.""",
    """Every route gets the same report: one line in words first, then the numbers: distance
    (and how far from a target), ascent and descent, surface where the service gives it, the
    share on ground the athlete's recorded runs passed before ("familiar", computed locally),
    the share the route covers twice ("retraced": out-and-back parts; a pure there-and-back is
    100 %), and the stored segments it passes with km from and to, in route order. --plot also
    writes a picture per route: map on top, elevation profile below.""",
    """Routes follow map data. A path can be closed, private, unlit or impassable: say so when
    you offer one, and ask the athlete to look at it before running it. Text by default,
    --json for everything.""",
)

app = typer.Typer()
route_app = typer.Typer(help=ROUTE_HELP, no_args_is_help=True)
app.add_typer(route_app, name="route")


class Prefer(StrEnum):
    familiar = "familiar"
    new = "new"
    none = "none"


PlotOption = Annotated[
    bool,
    typer.Option("--plot", help="Also write a picture per route (map and elevation) to out/."),
]

PLAN_HELP = _help(
    "Plan candidate routes and write each as a GPX file (and with --plot a picture) to out/.",
    """--start: usual (default: where most runs of the last 12 months started, from the
    database), LAT,LON, activity:ID (where that activity started; an id, a date YYYY-MM-DD or
    last), or a place name (the coach weather geocoder: city-level and coarse, it lands in a
    town centre, so prefer coordinates or usual). --distance KM: the length wanted. Without --to
    the route loops back to the start; --to takes the same forms as --start (activity:ID means
    where that activity ended).""",
    """--via LAT,LON passes a point; --via segment:KEY runs a stored segment from its start to
    its end (keys: coach analysis segments list). Repeat --via for several, in order. With via
    points and --distance the route through the points comes first; if it is shorter than the
    target, candidates add one detour each. The reported distance is always the distance the
    route actually has: a route through far-apart points can be longer than the target, and the
    words then say so ("2.3 km over the 10 km target"). "close to the target" means within
    0.25 km or 2.5 %, whichever is more.""",
    """--prefer familiar ranks routes on ground run before first, new the least known first,
    none (default) only by distance and shape. --candidates N (1-5) routes are shown; the
    service is asked at most 10 times per call. --trails uses openrouteservice's hiking profile,
    which prefers paths and trails over roads; --avoid-steps avoids stairs. Nothing else about
    terrain or surface is offered because the service would not honour it; the report says what
    surface each route has.""",
    """The words: shape "a loop" below 15 % retraced, "with out-and-back parts" from 15 %,
    "mostly out and back" from 50 %, "there and back" from 85 %; hills from ascent per km:
    "flat" below 5 m, "gently rolling" from 5, "hilly" from 12, "very hilly" from 25; ground:
    "new ground" below 10 % run before, "mostly new" from 10 %, "partly run before" from 40 %,
    "mostly run before" from 75 %; surface "mostly paved" or "mostly unpaved" from 75 %.""",
    """What leaves the machine: the routing service gets the coordinates of the start, the via
    points and the end (the usual start is often the athlete's home); with --plot the tile
    server sees which map tiles are fetched. The history, names and ids stay here. Needs
    COACH_ROUTE_API_KEY (or a self-hosted COACH_ROUTE_URL).""",
)


@contextmanager
def _maybe_db(json_out: bool) -> Iterator[Any]:
    """A database connection, or ``None`` when no database is configured."""
    import psycopg

    from thresy import db
    from thresy.config import ConfigError

    try:
        conn = db.connect()
    except ConfigError:
        yield None
        return
    except psycopg.Error as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise fail(f"database error: {detail}", as_json=json_out) from exc
    try:
        with conn:
            yield conn
    except psycopg.Error as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise fail(f"database error: {detail}", as_json=json_out) from exc


def _today() -> Any:
    from thresy.tools.dates import local_today

    return local_today()


def _need(conn: Any, what: str) -> Any:
    from thresy.tools.route import RouteError

    if conn is None:
        raise RouteError(f"{what} needs the database (COACH_DATABASE_URL is not set)")
    return conn


def _activity_ref(conn: Any, ref: str, *, end: bool) -> Any:
    from thresy.analysis.data import resolve_activity
    from thresy.tools.route import Point
    from thresy.tools.route_history import activity_point

    # last and a date mean runs; an explicit id may be any activity with positions
    sel = resolve_activity(_need(conn, "activity:ID"), ref, runs_only=True)
    lat, lon = activity_point(conn, sel.activity.id, last=end)
    where = "end" if end else "start"
    return Point(lat, lon, f"{where} of activity {sel.activity.id} ({sel.how})")


def resolve_point(conn: Any, text: str, *, end: bool = False) -> tuple[Any, Any]:
    """A start or end point; returns the point and the usual-start details (or ``None``)."""
    from thresy.tools.route import Point, RouteError, parse_coordinates
    from thresy.tools.route_history import usual_start

    value = (text or "usual").strip()
    if value.lower() == "usual":
        usual = usual_start(_need(conn, "the usual start"), _today())
        return Point(usual.lat, usual.lon, "the usual start"), usual
    coords = parse_coordinates(value)
    if coords:
        return Point(coords[0], coords[1], "given coordinates"), None
    if value.lower().startswith("activity:"):
        return _activity_ref(conn, value.split(":", 1)[1].strip(), end=end), None
    if value.lower().startswith("segment:"):
        raise RouteError(
            "segment:KEY is for --via; start and end take usual, LAT,LON, activity:ID or a place"
        )
    from thresy.tools import weather

    try:
        with weather.make_client() as client:
            place = weather.geocode(client, value)
    except weather.WeatherError as exc:
        raise RouteError(str(exc)) from exc
    label = f"place {place.label()}, found by name (city-level, coarse)"
    if place.alternatives:
        label += "; other places with this name: " + "; ".join(place.alternatives[:2])
    return Point(place.latitude, place.longitude, label), None


def resolve_via(conn: Any, text: str) -> Any:
    from thresy.analysis.segments import get_segment, list_segments
    from thresy.tools.route import RouteError, parse_coordinates
    from thresy.tools.route_planner import Via

    value = text.strip()
    if value.lower().startswith("segment:"):
        key = value.split(":", 1)[1].strip()
        seg = get_segment(_need(conn, "segment:KEY"), key)
        if seg is None:
            keys = ", ".join(s.key for s in list_segments(conn)) or "none defined"
            raise RouteError(f"no stored segment {key!r} (known: {keys})")
        return Via(
            ((seg.start_lat, seg.start_lon), (seg.end_lat, seg.end_lon)),
            f"segment {seg.key} ({seg.name})",
            seg.key,
        )
    coords = parse_coordinates(value)
    if coords is None:
        raise RouteError(f"--via takes LAT,LON or segment:KEY, not {text!r}")
    return Via((coords,), f"point {coords[0]:.5f}, {coords[1]:.5f}")


def _clean(value: Any) -> Any:
    """JSON-ready: numpy scalars to Python, private ``_keys`` dropped."""
    import numpy as np

    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


@contextmanager
def _tiles(settings: Any, wanted: bool) -> Iterator[Any]:
    """One tile source for all pictures of a command (shared cache and refusal), or ``None``
    when tiles are off or no picture is wanted."""
    from thresy.tools.route_plot import TileSource, make_tile_client

    if not wanted or not settings.tile_url:
        yield None
        return
    with make_tile_client() as client:
        yield TileSource(
            url=settings.tile_url,
            cache_dir=settings.tile_cache_dir,
            client=client,
            attribution=settings.tile_attribution,
        )


def _history(conn: Any, box: tuple[float, float, float, float], exclude: int | None = None) -> Any:
    from thresy.analysis.segments import list_segments
    from thresy.tools.route_history import known_cells

    if conn is None:
        return None, []
    return known_cells(conn, box, exclude_activity=exclude), list_segments(conn)


def _union_box(tracks: list[Any], *, plot: bool = False) -> tuple[float, float, float, float]:
    """The box the known ground is needed for: the routes, or with ``plot`` all of their
    pictures (so the background does not stop at the route's edge)."""
    from thresy.tools.route_plot import map_box

    boxes = [map_box(t.lat, t.lon) if plot else t.bbox() for t in tracks]
    return (
        min(b[0] for b in boxes),
        max(b[1] for b in boxes),
        min(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _picture(
    report: dict[str, Any], title: str, name: str, known: Any, tiles: Any, credits: list[str]
) -> tuple[str, dict[str, Any]]:
    from thresy.config import get_settings
    from thresy.tools.route import out_dir
    from thresy.tools.route_plot import plot_route

    path, info = plot_route(
        title,
        report,
        known=known,
        lang=get_settings().language,
        name=name,
        out_dir=out_dir(),
        tiles=tiles,
        credits=credits,
    )
    return str(path), info


def _title(n: int | None, report: dict[str, Any]) -> str:
    from thresy.analysis import plots
    from thresy.config import get_settings
    from thresy.tools.route_report import shape_label

    lang = get_settings().language
    lab = plots.labels(lang)
    bits = [f"{report['distance_km']:.1f} km"]
    if report["ascent_m"] is not None:
        bits.append(f"↑ {report['ascent_m']} m ↓ {report['descent_m']} m")
    bits.append(shape_label(report, lang))  # the report's shape, in the picture's language
    head = f"{lab['route']} {n}" if n is not None else lab["route"]
    return f"{head} · " + " · ".join(bits)


def _tile_note(info: dict[str, Any]) -> str | None:
    if info.get("tiles") == "unavailable":
        return (
            f"map tiles unavailable ({info.get('problem', 'no tile could be loaded')}):"
            " pictures drawn without a map background"
        )
    return None


def _known_errors() -> tuple[type[Exception], ...]:
    """The errors the route commands turn into one clean line."""
    from thresy.analysis.data import AnalysisError
    from thresy.config import ConfigError
    from thresy.tools.route import RouteError

    return (RouteError, AnalysisError, ConfigError)


@route_app.command("plan", help=PLAN_HELP)
def route_plan(
    start: Annotated[
        str, typer.Option("--start", help="usual, LAT,LON, activity:ID or a place name.")
    ] = "usual",
    distance: Annotated[
        float | None,
        typer.Option("--distance", min=0.5, max=100, help="Target length in km."),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="End somewhere else: usual, LAT,LON, activity:ID or a place."),
    ] = None,
    via: Annotated[
        list[str] | None,
        typer.Option("--via", help="LAT,LON or segment:KEY to pass, in order; repeatable."),
    ] = None,
    prefer: Annotated[
        Prefer, typer.Option("--prefer", help="Rank by ground run before: familiar, new, none.")
    ] = Prefer.none,
    candidates: Annotated[
        int, typer.Option("--candidates", min=1, max=5, help="How many routes to show.")
    ] = 3,
    trails: Annotated[
        bool, typer.Option("--trails", help="Prefer paths and trails (hiking profile).")
    ] = False,
    avoid_steps: Annotated[bool, typer.Option("--avoid-steps", help="Avoid stairs.")] = False,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    import logging

    from thresy.tools import route_planner as rp
    from thresy.tools.route import RouteSettings, out_dir, simplify_to, write_gpx
    from thresy.tools.route_provider import make_client, make_provider
    from thresy.tools.route_report import measure

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = RouteSettings()
    problem = settings.planning_problem()
    if problem:
        raise fail(problem, as_json=json_out)
    errors = _known_errors()
    with _maybe_db(json_out) as conn:
        try:
            start_point, usual = resolve_point(conn, start)
            end_point = resolve_point(conn, to, end=True)[0] if to else None
            if end_point is not None and _same(start_point, end_point):
                end_point = None
            vias = [resolve_via(conn, v) for v in via or []]
            request = rp.PlanRequest(
                start=start_point,
                end=end_point,
                vias=vias,
                distance_m=distance * 1000 if distance else None,
                candidates=candidates,
                trails=trails,
                avoid_steps=avoid_steps,
            )
            with make_client() as client:
                provider = make_provider(settings, client)
                planned = rp.plan(provider, request)
            tracks = [p.track for p in planned.paths]
            known, segments = _history(conn, _union_box(tracks, plot=plot))
        except errors as exc:
            raise fail(str(exc), as_json=json_out) from exc
    reports = []
    for path in planned.paths:
        report = measure(
            path.track,
            known=known,
            segments=segments,
            target_m=request.distance_m,
            surface=path.surface,
            waytype=path.waytype,
            elevation_source="the routing service's elevation model",
        )
        report["_path"] = path
        report["_cells"] = rp.route_cells(path.track.lat, path.track.lon, path.track.start)
        report["provider_distance_km"] = round(path.distance_m / 1000, 2)
        report["provider_ascent_m"] = None if path.ascent_m is None else round(path.ascent_m)
        reports.append(report)
    chosen = rp.rank(reports, prefer.value, request.distance_m)[:candidates]
    tag = _tag(request, prefer.value)
    notes = list(planned.notes)
    if len(chosen) < candidates:
        notes.append(f"{len(chosen)} distinct route(s) found, {candidates} asked for")
    with _tiles(settings, plot) as tiles:
        for n, report in enumerate(chosen, start=1):
            report["n"] = n
            report["warnings"] = _warnings(report, request, planned)
            stem = f"{tag}-{n}"
            track = simplify_to(report["_path"].track, tolerance_m=1.0, max_points=5000)
            file = write_gpx(track, out_dir() / f"{stem}.gpx", _gpx_name(n, report))
            report["file"] = str(file)
            report["file_name"] = file.name
            report["gpx_points"] = len(track)
            if plot:
                report["picture"], info = _picture(
                    report, _title(n, report), stem, known, tiles, [provider.attribution]
                )
                note = _tile_note(info)
                if note and note not in notes:
                    notes.append(note)
    data = {
        "start": {"lat": start_point.lat, "lon": start_point.lon, "how": start_point.label},
        "usual_start": None if usual is None else usual.describe(),
        "end": None
        if end_point is None
        else {"lat": end_point.lat, "lon": end_point.lon, "how": end_point.label},
        "vias": [{"how": v.label, "points": v.points} for v in vias],
        "target_km": distance,
        "prefer": prefer.value,
        "provider": {
            "name": provider.name,
            "profile": provider.profile,
            "method": planned.method,
            "requests": planned.requests,
            "routes_found": len(planned.paths),
            "attribution": provider.attribution,
        },
        "history": "computed" if known is not None else "no database: not computed",
        "candidates": [_clean(r) for r in chosen],
        "notes": notes,
    }
    emit(data, as_json=json_out, text=_plan_text(data, chosen, planned, provider, notes, plot))


def _same(a: Any, b: Any) -> bool:
    from thresy.tools.route import distance_m

    return distance_m((a.lat, a.lon), (b.lat, b.lon)) < 50


def _tag(request: Any, prefer: str) -> str:
    parts = [
        f"{request.start.lat:.4f},{request.start.lon:.4f}",
        "" if request.end is None else f"{request.end.lat:.4f},{request.end.lon:.4f}",
        ";".join(v.label for v in request.vias),
        str(request.distance_m),
        prefer,
        str(request.trails),
        str(request.avoid_steps),
    ]
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:6]
    km = f"-{request.distance_m / 1000:g}k" if request.distance_m else ""
    return f"route{km}-{digest}"


def _gpx_name(n: int, report: dict[str, Any]) -> str:
    shape = "loop" if report["loop"] else "point to point"
    return f"Route {n}: {report['distance_km']:.1f} km {shape}"


def _warnings(report: dict[str, Any], request: Any, planned: Any) -> list[str]:
    """Where the route misses what was asked: points it passes far from, segments not run."""
    import numpy as np

    res = report["_res"]
    out = []

    def gap(point: tuple[float, float]) -> float:
        dy = (res.lat - point[0]) * 111320.0
        dx = (res.lon - point[1]) * 111320.0 * np.cos(np.radians(point[0]))
        return float(np.min(np.hypot(dx, dy)))

    start = (request.start.lat, request.start.lon)
    if (g := gap(start)) > 75:
        out.append(
            f"starts {g:.0f} m from the requested start (the nearest path the service knows)"
        )
    for via in request.vias:
        for point in via.points:
            if (g := gap(point)) > 75:
                out.append(f"passes {g:.0f} m from {via.label}")
        if via.segment_key and not any(
            p["key"] == via.segment_key and p["direction"] == "forward" for p in report["segments"]
        ):
            out.append(
                f"does not run {via.label} from its start to its end as asked: the service took"
                " another way between them"
            )
    if request.end is not None and (g := gap((request.end.lat, request.end.lon))) > 75:
        out.append(f"ends {g:.0f} m from the requested end")
    return out


def _plan_text(
    data: dict[str, Any],
    chosen: list[dict[str, Any]],
    planned: Any,
    provider: Any,
    notes: list[str],
    plot: bool,
) -> str:
    from thresy.tools.route_report import candidates_text

    start = data["start"]
    kind = "a loop" if data["end"] is None else "a route"
    want = f" of about {data['target_km']:g} km" if data["target_km"] else ""
    lines = [
        f"Route candidates: {kind}{want} from {start['how']} ({start['lat']:.5f},"
        f" {start['lon']:.5f})"
    ]
    if data["usual_start"]:
        lines[-1] += f"; {data['usual_start']}"
    if data["end"]:
        end = data["end"]
        lines.append(f"to {end['how']} ({end['lat']:.5f}, {end['lon']:.5f})")
    for v in data["vias"]:
        lines.append(f"via {v['how']}")
    p = data["provider"]
    lines.append(
        f"{p['name']} ({p['profile']}, {p['method']}): {p['requests']} request(s),"
        f" {p['routes_found']} route(s) found, {len(chosen)} shown; preference: {data['prefer']}."
    )
    if data["history"] != "computed":
        lines.append("Familiarity and segments not computed: no database configured.")
    lines += ["", *candidates_text(chosen)]
    warn = [(c["n"], w) for c in chosen for w in c.get("warnings", [])]
    if warn or notes:
        lines += ["", "Notes:"]
        lines += [f"- route {n} {w}" for n, w in warn]
        lines += [f"- {note}" for note in notes]
    folder = Path(chosen[0]["file"]).parent if chosen else None
    lines += [
        "",
        f"Files in {folder}: "
        + ", ".join(c["file_name"] for c in chosen)
        + " (GPX 1.1 tracks without times; the athlete imports them into komoot or Garmin"
        " Connect; send one with coach send document).",
    ]
    if plot:
        names = [Path(c["picture"]).name for c in chosen if c.get("picture")]
        lines.append(f"Pictures in {folder}: " + ", ".join(names))
    lines += [
        "Routes follow OpenStreetMap data: a path can be closed, private, unlit or impassable;"
        " the athlete should look at a route before running it.",
        f"Route data: {provider.attribution}",
    ]
    return "\n".join(lines)


# show ------------------------------------------------------------------------------------------

SHOW_HELP = _help(
    "Report on a GPX file: the same numbers and words as coach route plan, and --plot.",
    """For a route the athlete planned elsewhere (komoot, Garmin Connect) and sent: files from
    the chat lie under inbox/<date>/. Reads the first track of the file (or its first route).
    Works without a routing service. Ascent and descent come from the file's elevation; a file
    without elevation gets them reported as unknown, not estimated. Surface is not in GPX files
    and is reported as unknown.""",
    "Nothing is sent anywhere, except map tiles with --plot.",
)


def _resolve_file(file: Path) -> Path:
    from thresy.config import get_settings

    if file.is_file() or file.is_absolute():
        return file
    candidate = get_settings().workspace_dir.expanduser() / file
    return candidate if candidate.is_file() else file


def _safe_stem(text: str) -> str:
    import re

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:60]
    return stem or "route"


@route_app.command("show", help=SHOW_HELP)
def route_show(
    file: Annotated[Path, typer.Argument(help="A GPX file, e.g. inbox/2030-03-04/route.gpx.")],
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    from thresy.tools.route import RouteSettings, read_gpx
    from thresy.tools.route_report import measure, single_text

    errors = _known_errors()
    path = _resolve_file(file)
    with _maybe_db(json_out) as conn:
        try:
            track = read_gpx(path)
            known, segments = _history(conn, _union_box([track], plot=plot))
            report = measure(track, known=known, segments=segments, elevation_source="the file")
        except errors as exc:
            raise fail(str(exc), as_json=json_out) from exc
    report["file"] = str(path)
    report["track_name"] = track.name
    report["points"] = len(track)
    notes = []
    with _tiles(RouteSettings(), plot) as tiles:
        if plot:
            report["picture"], info = _picture(
                report, _title(None, report), f"route-{_safe_stem(path.stem)}", known, tiles, []
            )
            if note := _tile_note(info):
                notes.append(note)
    report["notes"] = notes
    named = f', track "{track.name}"' if track.name else ""
    lines = [f"Route file: {path}{named}, {len(track)} points", *single_text(report)]
    if known is None:
        lines.append("familiar and segments: not computed (no database configured)")
    lines += [f"note: {n}" for n in notes]
    if plot:
        lines.append(f"picture: {report['picture']}")
    emit(_clean(report), as_json=json_out, text="\n".join(lines))


# export ----------------------------------------------------------------------------------------

EXPORT_HELP = _help(
    "Write a past activity as a GPX route to out/, to run it again or share the course.",
    """ACTIVITY is an id, a date YYYY-MM-DD (the longest run that day) or last (default). The
    file holds positions and elevation only (no times, no heart rate, no name of the
    activity), simplified to points within 3 m of the recorded track (at most 2500). The report
    is the one of coach route show; "familiar" there counts the ground of other runs only. Works
    without a routing service; nothing is sent anywhere, except map tiles with --plot.""",
)


@route_app.command("export", help=EXPORT_HELP)
def route_export(
    activity: Annotated[
        str, typer.Argument(help="Activity id, a date YYYY-MM-DD, or last.", show_default=False)
    ] = "last",
    all_types: Annotated[
        bool, typer.Option("--all-types", help="Any activity type, not only runs.")
    ] = False,
    plot: PlotOption = False,
    json_out: JsonOption = False,
) -> None:
    from thresy.analysis.data import load_records, resolve_activity, smooth_altitude
    from thresy.tools.route import RouteError, RouteSettings, Track, out_dir, simplify_to, write_gpx
    from thresy.tools.route_report import measure, single_text

    errors = _known_errors()
    with _maybe_db(json_out) as conn:
        try:
            sel = resolve_activity(
                _need(conn, "coach route export"), activity, runs_only=not all_types
            )
            act = sel.activity
            df = load_records(conn, act.id, ["lat", "lon", "alt"])
            alt = smooth_altitude(df)
            pos = (df["lat"].notna() & df["lon"].notna()).to_numpy()
            if pos.sum() < 2:
                raise RouteError(f"activity {act.id} has no recorded positions (indoors?)")
            full = Track(df["lat"].to_numpy()[pos], df["lon"].to_numpy()[pos], alt[pos])
            track = simplify_to(full, tolerance_m=3.0, max_points=2500)
            known, segments = _history(conn, _union_box([track], plot=plot), exclude=act.id)
            report = measure(
                track, known=known, segments=segments, elevation_source="the recorded altitude"
            )
        except errors as exc:
            raise fail(str(exc), as_json=json_out) from exc
    kind = "Run" if act.is_run else "Activity"
    file = write_gpx(
        track,
        out_dir() / f"activity-{act.id}.gpx",
        f"{kind} {act.local_date}, {report['distance_km']:.1f} km",
    )
    report.update(
        {
            "activity": {"id": act.id, "date": act.local_date, "how": sel.how},
            "file": str(file),
            "file_name": file.name,
            "points": len(track),
            "recorded_points": int(pos.sum()),
            "recorded_distance_km": None if not act.distance_m else round(act.distance_m / 1000, 2),
        }
    )
    notes = []
    with _tiles(RouteSettings(), plot) as tiles:
        if plot:
            report["picture"], info = _picture(
                report, _title(None, report), f"activity-{act.id}", known, tiles, []
            )
            if note := _tile_note(info):
                notes.append(note)
    report["notes"] = notes
    recorded = (
        f"; the watch recorded {report['recorded_distance_km']:.2f} km"
        if report["recorded_distance_km"]
        else ""
    )
    lines = [
        f"Exported {act.label}{'' if sel.how == 'id' else f' [{sel.how}]'} to {file}:"
        f" {len(track)} of"
        f" {int(pos.sum())} positions kept (within 3 m of the track){recorded}.",
        *single_text(report),
    ]
    if known is None:
        lines.append("familiar and segments: not computed (no database configured)")
    lines += [f"note: {n}" for n in notes]
    if plot:
        lines.append(f"picture: {report['picture']}")
    emit(_clean(report), as_json=json_out, text="\n".join(lines))
