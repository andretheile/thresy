"""coach route end to end: synthetic runs in the test database, the agent's role, and fakes for
the routing service, the geocoder and the tile server (no test reaches the network).

The made-up history: six runs out and back 3 km north of ORIGIN (the usual start), two runs from
a point 5 km east (one north, one south; the newest activity is the north one), and a bike
ride on a line 1.5 km east of ORIGIN, which must not count as ground run before. A stored
segment covers km 1 to 2 of the northern line."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.analysis.synth import T0, Phase, put_activity, records
from thresy.analysis.segments import SegmentDef, upsert_segments
from thresy.tools import route_plot, route_provider, weather
from thresy.tools.route import RouteError, Track, read_gpx, write_gpx
from thresy.tools.route_history import known_cells, usual_start
from thresy.tools.route_planner import MAX_REQUESTS

from .route_fakes import ORIGIN, FakeORS, FakeTiles, mock_client, offset
from .test_send import coach
from .test_weather import FakeOpenMeteo

pytestmark = pytest.mark.db

KEY = "test-route-key-0123456789abcdef"
EAST = offset(ORIGIN, 5000, 0)
GPX_NS = "{http://www.topografix.com/GPX/1/1}"
OUT_AND_BACK = [Phase(1000, speed=3.0, hr=140), Phase(1000, speed=3.0, hr=140, direction=-1)]
HILL = SegmentDef(
    "hill",
    "Hill climb",
    *offset(ORIGIN, 0, 1000),
    *offset(ORIGIN, 0, 2000),
    dist_min_m=950,
    dist_max_m=1050,
    uphill=False,
)


@pytest.fixture
def history(conn) -> dict[str, int]:
    for n in range(6):
        t0 = T0 + timedelta(days=n)
        put_activity(conn, 100 + n, records(OUT_AND_BACK, t0=t0), t0=t0)
    t0 = T0 + timedelta(days=10)
    put_activity(
        conn, 200, records([Phase(900, speed=3.0, direction=-1)], t0=t0, origin=EAST), t0=t0
    )
    t0 = T0 + timedelta(days=12)
    put_activity(conn, 201, records([Phase(900, speed=3.0)], t0=t0, origin=EAST), t0=t0)
    ride = records([Phase(1000, speed=6.0, east_m=1500)], t0=T0 + timedelta(days=11))
    put_activity(conn, 300, ride, t0=T0 + timedelta(days=11), type_key="cycling")
    upsert_segments(conn, [HILL])
    return {"newest": 201}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    ws = tmp_path / "workspace"
    (ws / "out").mkdir(parents=True)
    monkeypatch.chdir(ws)  # the agent's working directory; also: no .env here
    return ws


@pytest.fixture
def env(test_db, workspace: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "COACH_DATABASE_URL": test_db.agent_url,
        "COACH_WORKSPACE_DIR": str(workspace),
        "COACH_TZ": "UTC",
        "COACH_ROUTE_API_KEY": KEY,
        "COACH_ROUTE_TILE_URL": "none",
        "COACH_ROUTE_TILE_CACHE_DIR": str(tmp_path / "tiles"),
    }


@pytest.fixture
def ors(monkeypatch) -> FakeORS:
    fake = FakeORS()
    monkeypatch.setattr(route_provider, "make_client", lambda: mock_client(fake))
    return fake


@pytest.fixture
def tiles(monkeypatch) -> FakeTiles:
    fake = FakeTiles()
    monkeypatch.setattr(route_plot, "make_tile_client", lambda: mock_client(fake))
    return fake


def run(args: list[str], env: dict[str, str]) -> Any:
    result = coach(["route", *args], env)
    assert result.exit_code == 0, result.output
    return result


def gpx_track(
    path: Path,
    points_m: list[tuple[float, float]],
    *,
    ele: bool = True,
    origin: tuple[float, float] = ORIGIN,
) -> Path:
    """A GPX file through points given in metres east and north of ``origin``, every 20 m."""
    import numpy as np

    from thresy.tools.route import resample

    pts = [offset(origin, e, n) for e, n in points_m]
    coarse = Track(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
    dense = resample(coarse, 20.0)
    height = 100 + (dense.lat - origin[0]) * 111320 * 0.02 if ele else None
    return write_gpx(Track(dense.lat, dense.lon, height), path, "Synthetic route")


# History ---------------------------------------------------------------------------------------


def test_usual_start_is_where_most_runs_start(conn, history) -> None:
    usual = usual_start(conn, T0.date())
    assert abs(usual.lat - ORIGIN[0]) * 111320 < 20 and abs(usual.lon - ORIGIN[1]) * 111320 < 20
    assert (usual.near, usual.runs) == (6, 8)
    assert "6 of 8 runs since" in usual.describe()


def test_usual_start_needs_history(conn) -> None:
    with pytest.raises(RouteError, match="no runs with recorded positions"):
        usual_start(conn, T0.date())


def test_known_ground_counts_runs_only(conn, history) -> None:
    import numpy as np

    north = [offset(ORIGIN, 0, d) for d in range(0, 3000, 50)]
    ride = [offset(ORIGIN, 1500, d) for d in range(0, 3000, 50)]
    lat = np.array([p[0] for p in north + ride])
    lon = np.array([p[1] for p in north + ride])
    known = known_cells(conn, (lat.min(), lat.max(), lon.min(), lon.max()))
    familiar = known.familiar(lat, lon)
    assert familiar[: len(north)].all()
    assert not familiar[len(north) :].any()


# show ------------------------------------------------------------------------------------------


def test_show_a_gpx_the_athlete_sent(conn, history, workspace, env) -> None:
    file = gpx_track(
        workspace / "inbox" / "2030-03-20" / "planned.gpx",
        [(0, 0), (0, 2500), (1200, 2500), (1200, 0), (0, 0)],
    )
    result = run(["show", "inbox/2030-03-20/planned.gpx"], env)
    out = result.output
    assert out.startswith('Route file: inbox/2030-03-20/planned.gpx, track "Synthetic route"')
    words = out.splitlines()[1]
    # 50 m up on 7.4 km; 2.5 of 7.4 km on the northern line
    assert words.startswith("In words: a loop, 7.4 km; gently rolling (")
    assert "mostly new ground (3" in words and "passes Hill climb (km 1.0-2.0)" in words
    assert "surface: not known for this route" in out
    assert "km 1.00-2.00 Hill climb (hill), 1.00 km" in out
    data = json.loads(run(["show", str(file), "--json"], env).output)
    assert data["familiar_share"] == pytest.approx(2.5 / 7.4, abs=0.05)
    assert data["segments"][0]["key"] == "hill" and data["ascent_m"] > 0
    assert not any(k.startswith("_") for k in data)


def test_show_without_elevation_or_database_and_with_a_picture(workspace, env) -> None:
    gpx_track(workspace / "inbox" / "flat.gpx", [(0, 0), (2000, 0)], ele=False)
    no_db = {k: v for k, v in env.items() if k != "COACH_DATABASE_URL"}
    out = run(["show", "inbox/flat.gpx", "--plot"], no_db).output
    assert "ascent and descent: unknown (the route has no elevation)" in out
    assert "ascent unknown (no elevation)" in out.splitlines()[1]
    assert "not computed (no database configured)" in out
    assert (workspace / "out" / "route-flat.png").stat().st_size > 10_000


def test_show_refuses_a_broken_file(workspace, env) -> None:
    (workspace / "broken.gpx").write_text("<gpx>")
    result = coach(["route", "show", "broken.gpx"], env)
    assert result.exit_code == 1 and "not a readable GPX file" in result.output


# plan ------------------------------------------------------------------------------------------


def test_plan_a_loop_from_the_usual_start(conn, history, workspace, env, ors) -> None:
    data = json.loads(run(["plan", "--distance", "10", "--json"], env).output)
    assert data["start"]["how"] == "the usual start"
    assert abs(data["start"]["lat"] - ORIGIN[0]) * 111320 < 20
    assert "6 of 8 runs" in data["usual_start"]
    assert data["provider"]["requests"] == len(ors.requests) <= MAX_REQUESTS
    assert all(len(b["coordinates"]) == 1 for b in ors.bodies)  # round trips from the start
    candidates = data["candidates"]
    assert len(candidates) == 3
    for n, c in enumerate(candidates, start=1):
        assert c["n"] == n and c["close_to_target"]
        assert c["summary"].startswith("a loop, ")
        assert 0 <= c["familiar_share"] <= 1 and c["retraced_share"] < 0.1
        assert c["surface"] == {"paved": 0.6, "unpaved": 0.35, "unknown": 0.05}
        root = ET.parse(c["file"]).getroot()
        assert root.attrib["version"] == "1.1"
        assert len(root.findall(f"{GPX_NS}trk")) == 1
        assert "<time" not in Path(c["file"]).read_text()
        assert not any(k.startswith("_") for k in c)
    assert len({c["file"] for c in candidates}) == 3
    text = run(["plan", "--distance", "10"], env).output
    assert text.index("In words (from the numbers below):") < text.index("up/down m")
    assert "1. route-10k-" in text and "close to the 10 km target" in text
    assert "Route data: © openrouteservice.org by HeiGIT" in text
    assert "look at a route before running it" in text


def test_plan_through_a_segment_with_a_distance(conn, history, workspace, env, ors) -> None:
    args = ["plan", "--via", "segment:hill", "--distance", "9", "--prefer", "familiar", "--json"]
    data = json.loads(run(args, env).output)
    seg_start = [round(HILL.start_lon, 6), round(HILL.start_lat, 6)]
    seg_end = [round(HILL.end_lon, 6), round(HILL.end_lat, 6)]
    for body in ors.bodies:
        coords = body["coordinates"]
        assert coords[coords.index(seg_start) + 1] == seg_end
    first = data["candidates"][0]
    assert any(p["key"] == "hill" and p["direction"] == "forward" for p in first["segments"])
    assert not any("does not run" in w for w in first["warnings"])
    assert data["provider"]["method"] == "detours"
    # ranked by ground run before, in steps of 10 %
    shares = [round(c["familiar_share"], 1) for c in data["candidates"] if c["close_to_target"]]
    assert shares == sorted(shares, reverse=True)


def test_plan_says_when_a_segment_was_not_run_as_asked(conn, history, workspace, env, ors) -> None:
    # The fake service takes a dogleg between points 1.5 km apart: not the stored segment.
    ridge = SegmentDef(
        "ridge",
        "Ridge",
        *offset(ORIGIN, -500, 0),
        *offset(ORIGIN, -500, 1500),
        dist_min_m=1450,
        dist_max_m=1550,
    )
    upsert_segments(conn, [ridge])
    out = run(["plan", "--via", "segment:ridge", "--candidates", "1"], env).output
    assert "- route 1 does not run segment ridge (Ridge) from its start to its end as asked" in out


def test_plan_from_a_place_name_and_to_a_point(
    conn, history, workspace, env, ors, monkeypatch
) -> None:
    geo = FakeOpenMeteo()
    monkeypatch.setattr(weather, "make_client", lambda: mock_client(geo))
    end = offset((10.5, 20.25), 3000, 0)
    data = json.loads(
        run(["plan", "--start", "Springfield", "--to", f"{end[0]},{end[1]}", "--json"], env).output
    )
    assert "city-level, coarse" in data["start"]["how"]
    assert ors.bodies[0]["coordinates"][0] == [20.25, 10.5]
    assert ors.bodies[0]["alternative_routes"]["target_count"] == 3
    assert not data["candidates"][0]["loop"]
    assert data["candidates"][0]["summary"].startswith("point to point, ")


def test_plan_from_where_an_activity_started_or_ended(conn, history, workspace, env, ors) -> None:
    # activity:last is the newest run (201, from EAST northwards), not the newer-dated ride
    data = json.loads(
        run(
            ["plan", "--start", "activity:last", "--distance", "5", "--candidates", "1", "--json"],
            env,
        ).output
    )
    assert "start of activity 201 (last run)" in data["start"]["how"]
    assert ors.bodies[0]["coordinates"][0] == [round(EAST[1], 6), round(EAST[0], 6)]
    ors.bodies.clear()
    data = json.loads(
        run(["plan", "--to", "activity:201", "--candidates", "1", "--json"], env).output
    )
    assert data["end"]["how"] == "end of activity 201 (id)"
    lon, lat = ors.bodies[0]["coordinates"][-1]
    assert lon == round(EAST[1], 6)
    assert abs(lat - EAST[0]) * 111320 == pytest.approx(2700, abs=10)  # 900 s at 3 m/s north


def test_plan_with_pictures_and_tiles_from_the_cache(
    conn, history, workspace, env, ors, tiles
) -> None:
    with_tiles = {
        **env,
        "COACH_ROUTE_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png",
        "COACH_LANGUAGE": "de",
    }
    data = json.loads(
        run(["plan", "--distance", "6", "--candidates", "2", "--plot", "--json"], with_tiles).output
    )
    pictures = [Path(c["picture"]) for c in data["candidates"]]
    assert all(p.is_file() and p.parent == workspace / "out" for p in pictures)
    first = len(tiles.requests)
    assert 0 < first <= 2 * route_plot.MAX_TILES
    assert all(r.url.host == "tiles.example" for r in tiles.requests)
    run(["plan", "--distance", "6", "--candidates", "2", "--plot"], with_tiles)
    assert len(tiles.requests) == first  # every tile from the cache
    assert not data["notes"]


def test_plan_tiles_refused_still_draws(conn, history, workspace, env, ors, tiles) -> None:
    tiles.status = 403
    with_tiles = {**env, "COACH_ROUTE_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png"}
    out = run(["plan", "--distance", "6", "--candidates", "1", "--plot"], with_tiles).output
    assert "map tiles unavailable (tiles.example answered HTTP 403)" in out
    assert len(tiles.requests) == 1
    assert next((workspace / "out").glob("route-6k-*-1.png")).is_file()


def test_plan_needs_a_routing_service(conn, history, workspace, env, ors) -> None:
    no_key = {k: v for k, v in env.items() if k != "COACH_ROUTE_API_KEY"}
    result = coach(["route", "plan", "--distance", "10"], no_key)
    assert result.exit_code == 1
    assert "set COACH_ROUTE_API_KEY" in result.output and ors.requests == []
    result = coach(
        ["route", "plan", "--distance", "10", "--json"], {**env, "COACH_ROUTE_PROVIDER": "none"}
    )
    assert "switched off" in json.loads(result.output)["error"]


def test_service_errors_are_one_clean_line_without_the_key(
    conn, history, workspace, env, ors
) -> None:
    ors.status, ors.error = 400, {"code": 2003, "message": f"bad header {KEY}"}
    result = coach(["route", "plan", "--distance", "10"], env)
    assert result.exit_code == 1
    assert result.output.startswith("error: the routing service answered HTTP 400, code 2003")
    assert KEY not in result.output and "<redacted>" in result.output
    ors.status, ors.error = 403, "Quota exceeded"
    result = coach(["route", "plan", "--distance", "10", "--json"], env)
    assert "daily quota is used up" in json.loads(result.output)["error"]


def test_plan_reports_points_and_segments_it_could_not_use(
    conn, history, workspace, env, ors
) -> None:
    result = coach(["route", "plan", "--via", "segment:nope", "--distance", "5"], env)
    assert result.exit_code == 1 and "no stored segment 'nope' (known: hill)" in result.output
    result = coach(["route", "plan", "--start", "segment:hill"], env)
    assert result.exit_code == 1 and "segment:KEY is for --via" in result.output
    result = coach(["route", "plan"], env)
    assert result.exit_code == 1 and "a loop needs --distance" in result.output


# export ----------------------------------------------------------------------------------------


def test_export_a_past_run(conn, history, workspace, env) -> None:
    data = json.loads(run(["export", "last", "--json"], env).output)
    assert data["activity"]["id"] == history["newest"]
    path = Path(data["file"])
    assert path == workspace / "out" / "activity-201.gpx"
    text = path.read_text()
    for absent in ("<time", "<hr", "<extensions", "Synthetic Run"):
        assert absent not in text
    assert data["points"] < data["recorded_points"] and data["points"] <= 2500
    assert data["distance_km"] == pytest.approx(2.7, abs=0.05)
    # The other run from the same start went south: only the start is ground run before.
    assert data["familiar_share"] < 0.1
    out = run(["export", "100"], env).output
    assert out.startswith("Exported ") and "out/activity-100.gpx" in out
    assert "familiar: 100%" in out  # the five other runs on the same line
    # An out-and-back run keeps its turning point: the file is 6 km, not a dot at the start.
    words = out.splitlines()[1]
    assert words.startswith("In words: there and back on the same way (")
    assert ", 6.0 km;" in words
    back = read_gpx(workspace / "out" / "activity-100.gpx")
    assert 3 <= len(back) < 50


def test_export_refuses_an_activity_without_positions(conn, history, workspace, env) -> None:
    from dataclasses import replace

    t0 = T0 + timedelta(days=30)
    recs = [replace(r, lat=None, lon=None) for r in records([Phase(300)], t0=t0)]
    put_activity(conn, 400, recs, t0=t0)
    result = coach(["route", "export", "400"], env)
    assert result.exit_code == 1 and "no recorded positions" in result.output


def test_route_help(env) -> None:
    result = coach(["route", "--help"], env)
    assert result.exit_code == 0
    for command in ("plan", "show", "export"):
        assert command in result.output
    assert "writes to no account" in " ".join(result.output.split())
    plan_help = " ".join(coach(["route", "plan", "--help"], env).output.split())
    assert "city-level and coarse" in plan_help and "segment:KEY" in plan_help
