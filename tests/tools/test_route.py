"""coach route without a database: settings, GPX, measures, words, the routing service, the
planner, tiles. No test reaches the network: the routing service and the tile server are
:mod:`tests.tools.route_fakes`. Coordinates are invented."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from thresy.analysis.segments import SegmentDef
from thresy.tools import route_planner as rp
from thresy.tools import route_plot as rplot
from thresy.tools import route_report as rr
from thresy.tools.route import (
    HOSTED_ORS_URL,
    OSM_TILE_URL,
    Point,
    RouteError,
    RouteSettings,
    Track,
    agent_route_env,
    gpx_xml,
    parse_coordinates,
    read_gpx,
    resample,
    simplify_to,
    write_gpx,
)
from thresy.tools.route_history import Grid, Known
from thresy.tools.route_provider import OpenRouteService, RouteQuery, RoutingError

from .route_fakes import ORIGIN, FakeORS, FakeTiles, mock_client, offset

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
KEY = "test-route-key-0123456789abcdef"


def settings(**values: str) -> RouteSettings:
    return RouteSettings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def no_route_env(monkeypatch) -> None:
    """None of the developer's COACH_ROUTE_* values."""
    import os

    for name in list(os.environ):
        if name.startswith("COACH_ROUTE_"):
            monkeypatch.delenv(name)


def track(points_m: list[tuple[float, float]], ele: list[float] | None = None) -> Track:
    """A track through points given in metres east and north of ORIGIN."""
    pts = [offset(ORIGIN, east, north) for east, north in points_m]
    return Track(
        np.array([p[0] for p in pts]),
        np.array([p[1] for p in pts]),
        None if ele is None else np.array(ele, dtype=float),
    )


SQUARE = [(0, 0), (2500, 0), (2500, 2500), (0, 2500), (0, 0)]  # a 10 km loop
HILL = SegmentDef(
    "hill",
    "Hill climb",
    *offset(ORIGIN, 2500, 500),
    *offset(ORIGIN, 2500, 2000),
    dist_min_m=1400,
    dist_max_m=1600,
)


# Settings --------------------------------------------------------------------------------------


def test_settings_defaults_and_planning_problems() -> None:
    s = settings()
    assert s.base_url == HOSTED_ORS_URL and s.hosted
    assert s.tile_url == OSM_TILE_URL
    assert "COACH_ROUTE_API_KEY" in (s.planning_problem() or "")
    assert settings(route_api_key=KEY).planning_problem() is None
    self_hosted = settings(route_url="http://ors.internal:8082/ors/")
    assert self_hosted.planning_problem() is None and self_hosted.base_url.endswith("/ors")
    assert "switched off" in (settings(route_provider="none").planning_problem() or "")
    assert "unknown" in (settings(route_provider="osrm").planning_problem() or "")
    assert settings(route_tile_url="none").tile_url is None
    assert settings(route_tile_url="OFF").tile_url is None
    assert settings().tile_cache_dir.parts[-2:] == ("thresy", "tiles")


def test_agent_route_env_hands_over_only_set_values() -> None:
    assert agent_route_env(settings()) == {}
    env = agent_route_env(settings(route_api_key=f" {KEY} ", route_tile_url="none"))
    assert env == {"COACH_ROUTE_API_KEY": KEY, "COACH_ROUTE_TILE_URL": "none"}
    env = agent_route_env(settings(route_provider="openrouteservice", route_url="http://ors/ors"))
    assert env == {"COACH_ROUTE_PROVIDER": "openrouteservice", "COACH_ROUTE_URL": "http://ors/ors"}


def test_agent_env_of_the_bridge_includes_the_route_settings(tmp_path, monkeypatch) -> None:
    from thresy.bridge.agent import AgentService
    from thresy.config import Settings

    core = Settings(_env_file=None, agent_database_url="postgresql://agent@db/x", tz="UTC")
    service = SimpleNamespace(
        settings=core, bs=SimpleNamespace(home_location=None), workspace=tmp_path
    )
    env = AgentService.agent_env(service, 7)  # type: ignore[arg-type]
    assert not any(k.startswith("COACH_ROUTE_") for k in env)
    monkeypatch.setenv("COACH_ROUTE_API_KEY", KEY)
    monkeypatch.setenv("COACH_ROUTE_TILE_URL", "none")
    monkeypatch.chdir(tmp_path)  # no .env file
    env = AgentService.agent_env(service, 7)  # type: ignore[arg-type]
    assert env["COACH_ROUTE_API_KEY"] == KEY and env["COACH_ROUTE_TILE_URL"] == "none"
    assert env["COACH_DATABASE_URL"] == "postgresql://agent@db/x"


def test_parse_coordinates() -> None:
    assert parse_coordinates("10.5, 20.25") == (10.5, 20.25)
    assert parse_coordinates("Springfield") is None
    with pytest.raises(RouteError, match="out of range"):
        parse_coordinates("95, 20")


# GPX -------------------------------------------------------------------------------------------


def test_gpx_is_valid_1_1_with_one_named_track_and_nothing_personal(tmp_path: Path) -> None:
    t = track(SQUARE, [100, 110, 150, 140, 100])
    path = write_gpx(t, tmp_path / "r.gpx", "Route 1: 10.0 km loop")
    root = ET.parse(path).getroot()
    assert root.tag == f"{GPX_NS}gpx"
    assert root.attrib["version"] == "1.1" and root.attrib["creator"] == "Thresy"
    tracks = root.findall(f"{GPX_NS}trk")
    assert len(tracks) == 1
    assert tracks[0].find(f"{GPX_NS}name").text == "Route 1: 10.0 km loop"
    points = tracks[0].findall(f"{GPX_NS}trkseg/{GPX_NS}trkpt")
    assert len(points) == 5
    assert all(p.find(f"{GPX_NS}ele") is not None for p in points)
    text = path.read_text()
    for absent in ("<time", "<metadata", "<author", "<extensions", "<hr", "<desc"):
        assert absent not in text
    back = read_gpx(path)
    assert np.allclose(back.lat, t.lat, atol=1e-6) and np.allclose(back.ele, t.ele)
    assert back.name == "Route 1: 10.0 km loop"


def test_read_gpx_route_points_without_elevation(tmp_path: Path) -> None:
    lat0, lon0 = ORIGIN
    (tmp_path / "rte.gpx").write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="x" '
        'xmlns="http://www.topografix.com/GPX/1/1"><rte><name>Planned</name>'
        f'<rtept lat="{lat0}" lon="{lon0}"/><rtept lat="{lat0 + 0.01}" lon="{lon0}"/>'
        f'<rtept lat="{lat0 + 0.01}" lon="{lon0 + 0.01}"/></rte></gpx>'
    )
    t = read_gpx(tmp_path / "rte.gpx")
    assert len(t) == 3 and t.ele is None and t.name == "Planned"
    report = rr.measure(t, known=None, segments=[])
    assert report["ascent_m"] is None and report["descent_m"] is None
    assert "ascent unknown (no elevation)" in report["summary"]
    assert "unknown (the route has no elevation)" in "\n".join(rr.single_text(report))


def test_read_gpx_refuses_what_is_not_a_route(tmp_path: Path) -> None:
    (tmp_path / "bad.gpx").write_text("not xml at all")
    with pytest.raises(RouteError, match="not a readable GPX"):
        read_gpx(tmp_path / "bad.gpx")
    (tmp_path / "empty.gpx").write_text(gpx_xml(track([(0, 0)]), "x"))
    with pytest.raises(RouteError, match="at least two points"):
        read_gpx(tmp_path / "empty.gpx")
    with pytest.raises(RouteError, match="no such file"):
        read_gpx(tmp_path / "missing.gpx")


def test_simplify_keeps_the_corners_and_caps_the_points() -> None:
    dense = resample(track(SQUARE), 2.0)
    t = Track(dense.lat, dense.lon)
    assert len(t) > 4000
    simple = simplify_to(t, tolerance_m=3.0)
    assert len(simple) == 5
    assert abs(simple.length_m() - t.length_m()) < 1
    wiggly = Track(t.lat + np.sin(np.arange(len(t))) * 1e-4, t.lon)
    assert len(simplify_to(wiggly, tolerance_m=0.5, max_points=300)) <= 300


# Measures and words ----------------------------------------------------------------------------


def test_retraced_tells_a_loop_from_a_there_and_back() -> None:
    loop = rr.measure(track(SQUARE), known=None, segments=[])
    back = rr.measure(track([(0, 0), (5000, 0), (0, 0)]), known=None, segments=[])
    lolli = rr.measure(
        track(
            [
                (0, 0),
                (2000, 0),
                (2000, 1500),
                (5000, 1500),
                (5000, -1500),
                (2000, -1500),
                (2000, 0),
                (0, 0),
            ]
        ),
        known=None,
        segments=[],
    )
    assert loop["retraced_share"] < 0.02 and loop["summary"].startswith("a loop, 10.0 km")
    assert back["retraced_share"] > 0.95
    assert back["summary"].startswith("there and back on the same way (")
    # the 2 km stick is run twice: 4 km of 16 km
    assert lolli["retraced_km"] == pytest.approx(4.0, abs=0.2)
    assert lolli["summary"].startswith("a loop with out-and-back parts (25% retraced)")
    one_way = rr.measure(track([(0, 0), (3000, 0), (3000, 2000)]), known=None, segments=[])
    assert not one_way["loop"] and one_way["summary"].startswith("point to point, 5.0 km")


def test_title_and_report_share_one_source_for_the_shape(monkeypatch) -> None:
    from thresy.analysis.plots import LABELS
    from thresy.config import get_settings
    from thresy.tools.route_cli import _title

    # 6 km out, 3.5 km back: ends 2.5 km from the start, 7 of 9.5 km on the same way
    report = rr.measure(track([(0, 0), (6000, 0), (2500, 0)]), known=None, segments=[])
    assert rr.shape_kind(report) == "oneway_mostly_back"
    assert report["summary"].startswith("point to point, mostly retracing itself (73% retraced)")
    monkeypatch.chdir(Path(__file__).parent)  # no .env
    for lang, words in (
        ("de", "von A nach B, größtenteils auf demselben Weg zurück"),
        ("en", "point to point, mostly retracing itself"),
    ):
        monkeypatch.setenv("COACH_LANGUAGE", lang)
        get_settings.cache_clear()
        try:
            assert _title(1, report).endswith(" · " + words)
        finally:
            get_settings.cache_clear()
    kinds = {
        f"route_shape_{base}{tail}"
        for base in ("loop", "oneway")
        for tail in ("", "_parts", "_mostly_back", "_back")
    }
    assert all(kinds <= set(LABELS[lang]) for lang in LABELS)


def test_ascent_from_the_profile_and_hill_words() -> None:
    report = rr.measure(track(SQUARE, [100, 100, 180, 180, 100]), known=None, segments=[])
    assert report["ascent_m"] == pytest.approx(80, abs=5)
    assert report["descent_m"] == pytest.approx(80, abs=5)
    assert "gently rolling (" in report["summary"]
    flat = rr.measure(track(SQUARE, [100, 101, 102, 101, 100]), known=None, segments=[])
    assert "flat (" in flat["summary"]


@pytest.mark.parametrize(
    ("length_m", "words"),
    [
        (10_000, "close to the 10 km target (+0.0 km)"),
        (10_240, "close to the 10 km target (+0.2 km)"),
        (9_400, "0.6 km short of the 10 km target"),
        (12_300, "2.3 km over the 10 km target"),
    ],
)
def test_distance_words_never_claim_a_missed_target(length_m: float, words: str) -> None:
    side = length_m / 4
    t = track([(0, 0), (side, 0), (side, side), (0, side), (0, 0)])
    report = rr.measure(t, known=None, segments=[], target_m=10_000)
    assert words in report["summary"]
    assert report["close_to_target"] == ("close to" in words)


def test_segments_passed_in_route_order_with_direction() -> None:
    forward = rr.measure(track(SQUARE, [100, 100, 180, 180, 100]), known=None, segments=[HILL])
    (p,) = forward["segments"]
    assert (p["direction"], p["from_km"], p["to_km"]) == ("forward", 3.0, 4.5)
    assert p["gain_m"] > 0
    assert "passes Hill climb (km 3.0-4.5)" in forward["summary"]
    reverse = rr.measure(
        track(list(reversed(SQUARE)), [100, 180, 180, 100, 100]), known=None, segments=[HILL]
    )
    (q,) = reverse["segments"]
    assert q["direction"] == "reverse" and (q["from_km"], q["to_km"]) == (5.5, 7.0)
    assert "Hill climb in reverse, downhill (km 5.5-7.0)" in reverse["summary"]
    # Twice round the square: two passes, in order.
    twice = rr.measure(track(SQUARE + SQUARE[1:]), known=None, segments=[HILL])
    assert [s["from_km"] for s in twice["segments"]] == [3.0, 13.0]
    other = rr.measure(track([(0, 0), (-3000, 0), (-3000, -3000)]), known=None, segments=[HILL])
    assert other["segments"] == [] and "passes no stored segment" in other["summary"]


def test_surface_groups_and_words() -> None:
    report = rr.measure(
        track(SQUARE),
        known=None,
        segments=[],
        surface={"asphalt": 0.6, "gravel": 0.35, "unknown": 0.05},
        waytype={"path": 0.7, "street": 0.3},
    )
    assert report["surface"] == {"paved": 0.6, "unpaved": 0.35, "unknown": 0.05}
    assert report["summary"].endswith("mixed surface (paved 60%, unpaved 35%, unknown 5%)")
    text = "\n".join(rr.candidates_text([{**report, "n": 1, "file_name": "r-1.gpx"}]))
    assert text.index("In words") < text.index("up/down m")
    assert "path 70%, street 30%" in text


# The routing service ---------------------------------------------------------------------------


@pytest.fixture
def ors() -> tuple[OpenRouteService, FakeORS]:
    fake = FakeORS()
    return OpenRouteService(mock_client(fake), "https://ors.example/ors", KEY), fake


def test_request_shape_key_in_header_not_in_url(ors) -> None:
    service, fake = ors
    start = (10.0, 20.0)
    paths = service.route(
        RouteQuery(
            points=(start,), round_trip_m=8000, seed=3, round_trip_points=4, avoid_steps=True
        )
    )
    (request,) = fake.requests
    assert request.method == "POST"
    assert str(request.url) == "https://ors.example/ors/v2/directions/foot-walking/geojson"
    assert request.headers["Authorization"] == KEY
    assert KEY not in str(request.url)
    body = fake.bodies[0]
    assert body["coordinates"] == [[20.0, 10.0]]  # lon, lat
    assert body["elevation"] is True and body["instructions"] is False
    assert body["extra_info"] == ["surface", "waytype"]
    assert body["options"] == {
        "round_trip": {"length": 8000, "points": 4, "seed": 3},
        "avoid_features": ["steps"],
    }
    (path,) = paths
    assert path.track.ele is not None and path.distance_m > 0
    assert path.surface == {"asphalt": 0.6, "gravel": 0.35, "unknown": 0.05}
    assert path.waytype == {"path": 0.7, "street": 0.3}
    assert service.attribution.startswith("© openrouteservice.org by HeiGIT")
    service.route(RouteQuery(points=(start, (10.01, 20.0)), trails=True, alternatives=2))
    assert "/foot-hiking/" in str(fake.requests[-1].url)
    assert fake.bodies[-1]["alternative_routes"]["target_count"] == 3


def test_self_hosted_without_key_sends_no_authorization() -> None:
    fake = FakeORS()
    service = OpenRouteService(mock_client(fake), "http://ors:8082/ors", None)
    service.route(RouteQuery(points=((10.0, 20.0), (10.01, 20.0))))
    assert "Authorization" not in fake.requests[0].headers


@pytest.mark.parametrize(
    ("status", "error", "kind", "words"),
    [
        (429, "Rate Limit Exceeded", "rate_limit", "per-minute limit"),
        (403, "Daily quota reached or API key unauthorized", "auth", "COACH_ROUTE_API_KEY"),
        (403, "Quota exceeded", "quota", "daily quota is used up"),
        (401, "Unauthorized", "auth", "COACH_ROUTE_API_KEY is wrong"),
        (404, {"code": 2010, "message": "Could not find routable point"}, "point", "350 m"),
        (404, {"code": 2009, "message": "Route could not be found"}, "no_route", "no route"),
        (400, {"code": 2004, "message": "too long"}, "limit", "100 km"),
        (503, None, "unavailable", "try again later"),
    ],
)
def test_service_errors_become_one_line(ors, status, error, kind, words) -> None:
    service, fake = ors
    fake.status, fake.error = status, error
    with pytest.raises(RoutingError) as info:
        service.route(RouteQuery(points=((10.0, 20.0),), round_trip_m=5000))
    assert info.value.kind == kind and words in str(info.value)
    assert "\n" not in str(info.value)


def test_unreachable_service() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    service = OpenRouteService(mock_client(refuse), "https://ors.example/ors", KEY)
    with pytest.raises(RoutingError, match="unreachable") as info:
        service.route(RouteQuery(points=((10.0, 20.0),), round_trip_m=5000))
    assert KEY not in str(info.value)


def test_round_trip_limit_is_checked_before_asking(ors) -> None:
    service, fake = ors
    with pytest.raises(RoutingError, match="100 km"):
        service.route(RouteQuery(points=((10.0, 20.0),), round_trip_m=120_000))
    assert fake.requests == []


# The planner -----------------------------------------------------------------------------------

START = Point(*ORIGIN, "test start")


def test_round_trips_are_corrected_towards_the_target(ors) -> None:
    service, fake = ors
    planned = rp.plan(service, rp.PlanRequest(start=START, end=None, distance_m=10_000))
    lengths = [p.track.length_m() for p in planned.paths]
    assert sum(abs(x - 10_000) <= 250 for x in lengths) >= 3
    assert planned.requests == len(fake.requests) <= rp.MAX_REQUESTS
    seeds = [b["options"]["round_trip"]["seed"] for b in fake.bodies]
    assert seeds[:5] == [1, 2, 3, 4, 5]  # a sweep first, then corrections of the same seeds
    assert set(seeds[5:]) <= set(seeds[:5])


def test_the_request_cap_holds_when_nothing_converges(ors) -> None:
    service, fake = ors
    fake.fixed_round_trip_m = 31_000
    planned = rp.plan(
        service, rp.PlanRequest(start=START, end=None, distance_m=10_000, candidates=5)
    )
    assert len(fake.requests) == rp.MAX_REQUESTS
    assert any("no round trip within 0.25 km" in n for n in planned.notes)
    assert all(p.track.length_m() > 30_000 for p in planned.paths)


def test_detours_lengthen_a_route_through_points(ors) -> None:
    service, fake = ors
    via = rp.Via((offset(ORIGIN, 1500, 1500),), "a point")
    planned = rp.plan(service, rp.PlanRequest(start=START, end=None, vias=[via], distance_m=12_000))
    assert planned.method == "detours"
    base, *detours = planned.paths
    assert base.track.length_m() < 9000
    assert sum(abs(p.track.length_m() - 12_000) <= 300 for p in detours) >= 2
    assert all(len(b["coordinates"]) == 4 for b in fake.bodies[1:])  # one detour point each
    assert fake.bodies[1]["radiuses"][1:3] in ([1000, 350], [350, 1000])


def test_no_detour_inside_a_segment_and_no_shortening(ors) -> None:
    service, fake = ors
    seg = rp.Via((offset(ORIGIN, 0, 1000), offset(ORIGIN, 0, 2500)), "segment hill", "hill")
    rp.plan(service, rp.PlanRequest(start=START, end=None, vias=[seg], distance_m=14_000))
    seg_start, seg_end = ([round(p[1], 6), round(p[0], 6)] for p in seg.points)
    for body in fake.bodies:
        coords = body["coordinates"]
        i = coords.index(seg_start)
        assert coords[i + 1] == seg_end  # nothing between the segment's start and end
    fake.bodies.clear()
    far = rp.Via((offset(ORIGIN, 8000, 0),), "far")
    planned = rp.plan(service, rp.PlanRequest(start=START, end=None, vias=[far], distance_m=10_000))
    assert len(fake.bodies) == 1
    assert "already 19.2 km, more than the 10 km target" in planned.notes[0]


# A segment that starts 400 m from the start, run from 0.4 to 1.4 km without a warm-up.
CLOSE_SEGMENT = rp.Via((offset(ORIGIN, 0, 400), offset(ORIGIN, 0, 1400)), "segment near", "near")


def warm_request(warmup_m: float | None, **kw) -> rp.PlanRequest:
    values = {"distance_m": 10_000, "candidates": 3, **kw}
    return rp.PlanRequest(start=START, end=None, vias=[CLOSE_SEGMENT], warmup_m=warmup_m, **values)


def assert_segment_kept_whole(bodies: list[dict]) -> None:
    seg_start, seg_end = ([round(p[1], 6), round(p[0], 6)] for p in CLOSE_SEGMENT.points)
    for body in bodies:
        coords = body["coordinates"]
        assert coords[coords.index(seg_start) + 1] == seg_end  # no detour inside the segment


def test_warmup_puts_the_segment_later_and_still_aims_for_the_distance(ors) -> None:
    service, fake = ors
    plain = rp.plan(service, warm_request(None))
    assert rp.first_via_m(plain.paths[0], CLOSE_SEGMENT.points[0]) == pytest.approx(400, abs=30)
    fake.bodies.clear()
    planned = rp.plan(service, warm_request(3000))
    assert planned.requests == len(fake.bodies) <= rp.MAX_REQUESTS
    assert_segment_kept_whole(fake.bodies)
    good = [
        p
        for p in planned.paths
        if rp.first_via_m(p, CLOSE_SEGMENT.points[0]) >= 3000
        and abs(p.track.length_m() - 10_000) <= 250
    ]
    assert good, [round(p.track.length_m()) for p in planned.paths]
    # the warm-up detour comes before the segment; the distance detours after it
    for path in good:
        points = path.query.points
        assert points.index(CLOSE_SEGMENT.points[0]) == 2
    assert not any("warm-up" in n for n in planned.notes)


def test_warmup_that_cannot_be_reached_says_so(ors) -> None:
    service, fake = ors
    fake.refuse_detours = True  # no path near any detour point
    planned = rp.plan(service, warm_request(3000))
    assert planned.requests <= rp.MAX_REQUESTS
    assert all(rp.first_via_m(p, CLOSE_SEGMENT.points[0]) < 3000 for p in planned.paths)
    assert "the 3 km warm-up was not reached within the request cap" in planned.notes[0]
    assert "segment near at km 0.4" in planned.notes[0]
    assert_segment_kept_whole(fake.bodies)


def test_warmup_respects_the_request_cap(ors) -> None:
    service, fake = ors
    planned = rp.plan(service, warm_request(3000, candidates=5), max_requests=3)
    assert len(fake.bodies) == planned.requests == 3
    planned = rp.plan(service, warm_request(3000), max_requests=1)
    assert "the 3 km warm-up was not reached" in planned.notes[0]
    planned = rp.plan(
        service, rp.PlanRequest(start=START, end=None, distance_m=8000, warmup_m=2000)
    )
    assert "--warmup applies to the first --via point or segment" in planned.notes[-1]


def test_alternatives_for_start_and_end(ors) -> None:
    service, fake = ors
    end = Point(*offset(ORIGIN, 3000, 0), "end")
    planned = rp.plan(service, rp.PlanRequest(start=START, end=end, candidates=3))
    assert planned.method == "alternatives" and len(planned.paths) == 3
    assert len(fake.requests) == 1


def test_a_loop_needs_a_distance(ors) -> None:
    service, _ = ors
    with pytest.raises(RouteError, match="needs --distance"):
        rp.plan(service, rp.PlanRequest(start=START, end=None))


def _report(fam: float, retraced: float, off: float, cells: set) -> dict:
    return {
        "familiar_share": fam,
        "retraced_share": retraced,
        "off_target_km": off,
        "close_to_target": abs(off) <= 0.25,
        "_cells": cells,
    }


def test_rank_by_preference_and_drop_duplicates() -> None:
    a = _report(0.9, 0.05, 0.2, {(0, i) for i in range(100)})
    b = _report(0.1, 0.05, 0.0, {(1, i) for i in range(100)})
    c = _report(0.5, 0.05, 1.5, {(2, i) for i in range(100)})  # outside the tolerance
    dup = _report(0.85, 0.05, 0.1, {(0, i) for i in range(95)})
    assert rp.rank([a, b, c], "familiar", 10_000) == [a, b, c]
    assert rp.rank([a, b, c], "new", 10_000) == [b, a, c]
    assert rp.rank([a, b, c], "none", 10_000)[0] is b
    assert rp.rank([a, dup, b], "familiar", 10_000) == [a, b]


# Tiles -----------------------------------------------------------------------------------------


def test_zoom_keeps_the_tile_count_small() -> None:
    x, y = rplot.mercator(np.array([10.0, 10.05]), np.array([20.0, 20.06]))
    extent = (float(x[0]), float(x[1]), float(y[0]), float(y[1]))
    z = rplot.choose_zoom(extent)
    cols, rows = rplot.tile_range(extent, z)
    assert len(cols) * len(rows) <= rplot.MAX_TILES
    cols, rows = rplot.tile_range(extent, z + 1)
    assert len(cols) * len(rows) > rplot.MAX_TILES


def test_tile_cache_honours_headers_and_revalidates(tmp_path: Path) -> None:
    fake = FakeTiles(cache_control="max-age=3600")
    tiles = rplot.TileSource(OSM_TILE_URL, tmp_path, mock_client(fake), "© x")
    assert tiles.get(14, 1, 2) == fake.tile
    assert tiles.get(14, 1, 2) == fake.tile  # fresh: from the cache
    assert len(fake.requests) == 1 and tiles.from_cache == 1
    assert str(fake.requests[0].url) == "https://tile.openstreetmap.org/14/1/2.png"
    assert "no-cache" not in str(fake.requests[0].headers).lower()
    meta = next(tmp_path.rglob("2.json"))
    data = json.loads(meta.read_text())
    assert 3500 < data["fresh_until"] - time.time() <= 3600
    data["fresh_until"] = time.time() - 1  # expired: a conditional request, answered 304
    meta.write_text(json.dumps(data))
    assert tiles.get(14, 1, 2) == fake.tile
    assert fake.requests[-1].headers["If-None-Match"] == '"tile-1"'
    assert tiles.revalidated == 1


def test_tiles_without_caching_headers_are_kept_a_week() -> None:
    assert rplot.freshness_s(httpx.Headers({})) == 7 * 86400
    assert rplot.freshness_s(httpx.Headers({"Cache-Control": "public, max-age=600"})) == 600
    assert rplot.freshness_s(httpx.Headers({"Cache-Control": "no-cache"})) == 0


def test_a_refusing_tile_server_is_not_asked_again(tmp_path: Path) -> None:
    fake = FakeTiles(status=429)
    tiles = rplot.TileSource(OSM_TILE_URL, tmp_path, mock_client(fake), "© x")
    x, y = rplot.mercator(np.array([10.0, 10.03]), np.array([20.0, 20.04]))
    assert rplot.mosaic(tiles, (float(x[0]), float(x[1]), float(y[0]), float(y[1]))) is None
    assert len(fake.requests) == 1 and tiles.problems == [
        "tile.openstreetmap.org answered HTTP 429"
    ]


def test_picture_with_tiles_and_without_elevation(tmp_path: Path) -> None:
    fake = FakeTiles()
    tiles = rplot.TileSource(OSM_TILE_URL, tmp_path / "cache", mock_client(fake), "© OSM")
    report = rr.measure(track(SQUARE, [100, 100, 180, 180, 100]), known=None, segments=[HILL])
    path, info = rplot.plot_route(
        "Route 1",
        report,
        known=None,
        lang="de",
        name="r1",
        out_dir=tmp_path,
        tiles=tiles,
        credits=["© ors"],
    )
    assert path == tmp_path / "r1.png" and path.stat().st_size > 10_000
    assert info["tiles"] == "drawn" and 0 < info["fetched"] <= rplot.MAX_TILES
    assert all(r.headers["User-Agent"].startswith("Thresy/") for r in fake.requests)
    bare = rr.measure(track(SQUARE), known=None, segments=[])
    path, info = rplot.plot_route(
        "Route 2", bare, known=None, lang="en", name="r2", out_dir=tmp_path
    )
    assert path.is_file() and info == {"tiles": "off"}


# The picture: familiarity on the line, earlier runs, labels -------------------------------------

STREET_M = 150


def known_from_lines(lines: list[tuple[tuple[float, float], tuple[float, float]]]) -> Known:
    """Known ground: the 30 m cells of runs along straight lines (metres from ORIGIN)."""
    lo, hi = offset(ORIGIN, -4500, -5000), offset(ORIGIN, 6000, 5000)
    grid = Grid(origin=lo)
    cells: set[tuple[int, int]] = set()
    for (e0, n0), (e1, n1) in lines:
        steps = int(max(abs(e1 - e0), abs(n1 - n0)) // 10) + 1
        pts = [
            offset(ORIGIN, e0 + (e1 - e0) * k / steps, n0 + (n1 - n0) * k / steps)
            for k in range(steps + 1)
        ]
        keys = grid.cells(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
        cells |= {(int(x), int(y)) for x, y in keys}
    return Known(grid, (lo[0], hi[0], lo[1], hi[1]), cells)


# Every street of a 150 m grid, 6 by 8 km, run; or three lines.
DENSE = known_from_lines(
    [((x, -4000), (x, 4000)) for x in range(-3000, 3001, STREET_M)]
    + [((-3000, y), (3000, y)) for y in range(-3900, 4000, STREET_M)]
)
SPARSE = known_from_lines([((0, -4000), (0, 4000)), ((-3000, 1500), (3000, 1500))])
# Half on the grid, half east of it; a stored segment from 400 m after the start.
PART_KNOWN = [
    (0, 0),
    (0, 1500),
    (2400, 1500),
    (4500, 1500),
    (4500, -1050),
    (2400, -1050),
    (0, -1050),
    (0, 0),
]
NEAR_START = SegmentDef(
    "near",
    "Hill near home",
    *offset(ORIGIN, 0, 400),
    *offset(ORIGIN, 0, 1400),
    dist_min_m=950,
    dist_max_m=1050,
)


@pytest.fixture
def drawn(monkeypatch) -> list:
    """The figures the pictures were drawn on (saved as usual)."""
    from thresy.analysis import plots

    figures: list = []
    save = plots._save

    def keep(fig, name, out_dir):
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        path = save(fig, name, out_dir)
        FigureCanvasAgg(fig)  # closing detached it; extents need a renderer
        figures.append(fig)
        return path

    monkeypatch.setattr(plots, "_save", keep)
    return figures


def hilly(points_m: list[tuple[float, float]]) -> Track:
    res = resample(track(points_m), 10.0)
    return Track(res.lat, res.lon, 120 + 60 * np.sin(res.cum / 1500))


def legend_texts(fig) -> list[str]:
    return [t.get_text() for legend in fig.legends for t in legend.get_texts()]


@pytest.mark.parametrize("history", ["dense", "sparse"])
@pytest.mark.parametrize("with_tiles", [False, True])
def test_familiar_ground_is_on_the_line_and_never_floods_the_map(
    tmp_path, drawn, history, with_tiles
) -> None:
    known = DENSE if history == "dense" else SPARSE
    report = rr.measure(hilly(PART_KNOWN), known=known, segments=[NEAR_START])
    assert 0.3 < report["familiar_share"] < 0.8  # part on it, part outside
    tiles = None
    if with_tiles:
        tiles = rplot.TileSource(OSM_TILE_URL, tmp_path / "c", mock_client(FakeTiles()), "© x")
    _, info = rplot.plot_route(
        "R", report, known=known, lang="en", name="r", out_dir=tmp_path, tiles=tiles
    )
    fig = drawn[-1]
    ax = fig.axes[0]
    legend = legend_texts(fig)
    assert {"Route, run before", "Route, new ground"} <= set(legend)
    styles = {line.get_linestyle() for line in ax.lines if line.get_color() == "#2a78d6"}
    assert {"-", "--"} <= styles  # solid where run before, dashed on new ground
    if with_tiles:
        assert info["tiles"] == "drawn"
        assert len(ax.images) == 1  # the map only: no known-ground overlay
        assert "Earlier runs" not in legend
    else:
        (layer,) = ax.images
        alpha = np.asarray(layer.get_array())[:, :, 3]
        assert 0 < alpha.max() <= 0.35  # faint
        # true cell size: no more cells drawn than are known, and a grid stays a network
        assert (alpha > 0).sum() <= len(known.cells)
        if history == "dense":
            assert (alpha > 0).mean() < 0.45  # 30 m lines every 150 m, not a filled area
        assert "Earlier runs" in legend


def test_steady_drawing_ignores_short_stretches() -> None:
    mask = np.array([1] * 30 + [0] * 4 + [1] * 30 + [0] * 40, dtype=bool)
    out = rplot.steady(mask, 10)
    assert out[:64].all() and not out[64:].any()
    assert rplot.steady(np.ones(5, dtype=bool), 10).all()


def _boxes(ax) -> list:
    from matplotlib.text import Annotation, Text

    renderer = ax.figure.canvas.get_renderer()
    out = []
    for t in ax.texts:
        if not t.get_text() or not t.get_visible():
            continue
        if isinstance(t, Annotation):
            t.update_positions(renderer)
        out.append((t.get_text(), Text.get_window_extent(t, renderer)))
    return out


@pytest.mark.parametrize("shape", ["loop", "point to point"])
def test_labels_stay_inside_and_clear_of_each_other(tmp_path, drawn, shape) -> None:
    points = PART_KNOWN if shape == "loop" else [*PART_KNOWN[:5], (3550, -1050)]
    report = rr.measure(hilly(points), known=SPARSE, segments=[NEAR_START])
    rplot.plot_route("R", report, known=SPARSE, lang="de", name="r", out_dir=tmp_path)
    ax_map, ax_profile = drawn[-1].axes
    for ax in (ax_map, ax_profile):
        frame = ax.get_window_extent(ax.figure.canvas.get_renderer())
        boxes = _boxes(ax)
        for text, box in boxes:
            assert box.x0 >= frame.x0 - 1 and box.x1 <= frame.x1 + 1, text
            assert box.y0 >= frame.y0 - 1 and box.y1 <= frame.y1 + 1, text
        for i, (a, box_a) in enumerate(boxes):
            for b, box_b in boxes[i + 1 :]:
                assert not box_a.overlaps(box_b), (a, b)
    names = [t for t, _ in _boxes(ax_map)]
    assert "Hill near home" in names
    assert ("Start/Ziel" if shape == "loop" else "Ziel") in names
    assert "Hill near home" in [t for t, _ in _boxes(ax_profile)]


def test_a_long_title_wraps_instead_of_widening_the_picture(tmp_path, drawn) -> None:
    from PIL import Image

    report = rr.measure(track(SQUARE, [100, 100, 180, 180, 100]), known=None, segments=[])
    short = "Route 1 · 10.0 km · ↑ 80 m ↓ 80 m · eine Runde"
    long = "Route 3 · 12.4 km · ↑ 143 m ↓ 143 m · " + rr.shape_label(
        {"retraced_share": 0.6, "loop": False}, "de"
    )
    assert len(long) > 75
    sizes = {}
    for name, title in (("short", short), ("long", long)):
        path, _ = rplot.plot_route(
            title, report, known=None, lang="de", name=name, out_dir=tmp_path
        )
        with Image.open(path) as img:
            sizes[name] = img.size
    assert sizes["long"][0] == sizes["short"][0]  # as wide as the map, no blank strip
    assert sizes["long"][1] > sizes["short"][1]  # a second line of title
    fig = drawn[-1]
    text = fig._suptitle.get_text()
    assert text == "Route 3 · 12.4 km · ↑ 143 m ↓ 143 m\n" + long.split(" · ")[-1]
    box = fig._suptitle.get_window_extent(fig.canvas.get_renderer())
    map_box = fig.axes[0].get_window_extent(fig.canvas.get_renderer())
    assert box.x1 <= map_box.x1 and box.y0 > map_box.y1  # inside the width, above the map
    assert drawn[0]._suptitle.get_text() == short  # a title that fits stays on one line


def test_a_title_shrinks_when_two_lines_are_not_enough() -> None:
    from thresy.analysis import plots

    plt = plots._plt()
    fig = plt.figure(figsize=(3, 2))

    def parts(n: int) -> str:
        return " · ".join(["Abschnitt"] * n)

    assert rplot.fit_title(fig, parts(4), 2.9, 13.0) == (parts(2) + "\n" + parts(2), 13.0)
    assert rplot.fit_title(fig, parts(6), 2.9, 13.0) == (parts(3) + "\n" + parts(3), 11.0)
    text, size = rplot.fit_title(fig, parts(12), 2.9, 13.0)  # more lines only at the minimum
    assert size == rplot.TITLE_MIN_PT and text.count("\n") == 3
    plt.close(fig)


def test_a_name_without_room_goes_into_the_legend(tmp_path, drawn, monkeypatch) -> None:
    monkeypatch.setattr(rplot._Labels, "place", lambda self, *args, **kw: None)
    report = rr.measure(track(SQUARE, [100, 100, 180, 180, 100]), known=None, segments=[HILL])
    rplot.plot_route("R", report, known=None, lang="en", name="r", out_dir=tmp_path)
    legend = legend_texts(drawn[-1])
    assert "Stored segment: Hill climb" in legend and "Start/finish" in legend


def test_labels_avoid_what_is_blocked() -> None:
    from thresy.analysis import plots

    plt = plots._plt()
    fig, ax = plt.subplots(figsize=(3, 3))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    labels = rplot._Labels(ax)
    labels.block_point(5, 4, 30)  # right under the first try
    blocked = labels.taken[0]
    ann = labels.place("Label", [(5, 5)], rplot.AROUND)
    assert ann is not None and not labels._extent(ann).overlaps(blocked)
    corner = labels.place("Corner label", [(0.3, 0.3)], rplot.AROUND)
    assert corner is not None and labels.fits(labels.taken.pop())  # inside, clear of the rest
    labels.taken.append(labels.frame)  # no room left anywhere
    assert labels.place("No room", [(5, 5)], rplot.AROUND) is None
    plt.close(fig)
