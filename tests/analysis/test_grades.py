"""``coach analysis grades``: gradients of fixed pieces, on synthetic courses whose gradients are
known, so every expected number can be worked out by hand. Coordinates and profiles are made
up; no test reaches the network."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from tests.analysis.synth import M_PER_DEG, ORIGIN, T0, Phase, put_activity, records
from thresy.analysis import grades as gr
from thresy.analysis.cli import app
from thresy.analysis.data import AnalysisError
from thresy.config import get_settings
from thresy.tools.route import Track, write_gpx

runner = CliRunner()

# (length m, gradient %): flat 550 m, 2 km at exactly 5 %, flat 950 m. The corners lie 50 m
# from the edges of the 100 m pieces, beyond the reach of either smoothing (15 samples at
# 2.5 m/s, 70 m), so the numbers hold for the arrays, the activity and the GPX file alike:
# pieces 0.0-0.5 flat, 0.5-0.6 at 2.5 % (half flat, half 5 %), 0.6-2.5 at 5.0 %, 2.5-2.6 at
# 2.5 %, 2.6-3.5 flat. 35 pieces, 100 m up.
KNOWN = [(550, 0.0), (2000, 5.0), (950, 0.0)]


def profile(parts: list[tuple[float, float]], step: float = 10.0) -> tuple[np.ndarray, ...]:
    """Distance and altitude (from 100 m) every ``step`` metres along the parts."""
    total = sum(length for length, _ in parts)
    dist = np.arange(0.0, total + 1e-6, step)
    alt = np.full(len(dist), 100.0)
    at, height = 0.0, 100.0
    for length, grade in parts:
        inside = (dist > at) & (dist <= at + length)
        alt[inside] = height + (dist[inside] - at) * grade / 100
        at, height = at + length, height + length * grade / 100
    return dist, alt


def course(parts: list[tuple[float, float]], label: str = "c") -> gr.Course:
    dist, alt = profile(parts)
    return gr.Course(dist, alt, label, "file", "file")


def by_band(result: dict) -> dict[str, float]:
    return {b["band"]: b["km"] for b in result["bands"]}


def by_class(result: dict) -> dict[str, float]:
    return {c["label"]: c["km"] for c in result["classes"] if c["km"]}


def gpx(path: Path, parts: list[tuple[float, float]], *, elevation: bool = True) -> Path:
    """A GPX file running north from a made-up origin with the given profile."""
    dist, alt = profile(parts)
    lat = ORIGIN[0] + dist / M_PER_DEG
    lon = np.full(len(dist), ORIGIN[1])
    write_gpx(Track(lat, lon, alt if elevation else None), path, path.stem)
    return path


def check_known(r: dict) -> None:
    """The numbers of :data:`KNOWN`, however it was read."""
    assert r["pieces"] == 35 and r["length_km"] == 3.5 and r["ascent_m"] == 100
    assert by_band(r) == {
        "steep_down": 0.0,
        "gentle_down": 0.0,
        "flat": 1.4,
        "gentle_up": 0.2,
        "steep_up": 1.9,
    }
    assert by_class(r) == {"-1 to +1": 1.4, "+1 to +3": 0.2, "+5 to +7": 1.9}
    assert [c["share_pct"] for c in r["classes"] if c["km"]] == [40.0, 5.7, 54.3]
    assert r["steepest_up"] == {"grade_pct": 5.0, "from_km": 0.6, "to_km": 0.7}
    assert r["steepest_down"] is None
    (climb,) = r["climbs"]
    assert climb == {
        "from_km": 0.5,
        "to_km": 2.6,
        "length_km": 2.1,
        "grade_pct": 4.8,
        "steepest_pct": 5.0,
        "pieces": 21,
        "gain_m": 100.0,
    }
    (steep,) = r["steep_up"]
    assert (steep["from_km"], steep["to_km"], steep["grade_pct"]) == (0.6, 2.5, 5.0)
    assert r["steep_down"] == [] and r["short_ramps"] == 0


# The method on arrays --------------------------------------------------------------------------


def test_classes_bands_and_climbs_of_a_known_course() -> None:
    result = gr.compare([course(KNOWN)])
    (r,) = result["courses"]
    check_known(r)
    assert r["uphill_km"] == 2.1 and r["uphill_share_pct"] == 60.0
    assert r["climb_kind"] == "long_gentle"  # 4.8 % on average: the half-steep end pieces
    assert r["summary"] == (
        "very hilly (29 m up per km, 100 m over 3.5 km); uphill 2.1 km (60 %), mostly steep "
        "(0.2 km at 1-5 %, 1.9 km at 5 % or more); 1 longer steep stretch (km 0.6-2.5, 1.9 km "
        "at 5.0 %); mostly long gentle climbs (longest 2.1 km at 4.8 %)"
    )
    assert result["likeness"] == [] and result["comparison"] == []


def test_two_km_at_exactly_five_percent_are_steep() -> None:
    dist, alt = profile([(2000, 5.0), (1000, 0.0)])
    p = gr.cut(dist, alt)
    assert len(p) == 30
    assert list(p.grade_pct) == [5.0] * 20 + [0.0] * 10
    th = gr.Thresholds()
    assert {b["band"]: b["km"] for b in gr.bands(p, th)}["steep_up"] == 2.0


def test_band_and_class_limits() -> None:
    g = np.array([-12, -5, -4.9, -1, -0.9, 0, 0.9, 1, 4.9, 5, 9, 20])
    assert list(gr.band_of(g, 1.0, 5.0)) == [0, 0, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4]
    # 2 % classes centred on 0: -1 and +1 leave the class around 0 like the flat band does;
    # the outer classes hold everything from 9 % on
    centres = gr.class_centres(2.0, 10.0)
    assert list(centres) == [-10, -8, -6, -4, -2, 0, 2, 4, 6, 8, 10]
    assert list(centres[gr.class_of(g, 2.0, 10.0)]) == [-10, -6, -4, -2, 0, 0, 0, 2, 4, 6, 10, 10]
    labels = [c["label"] for c in gr.classes(gr.cut(*profile(KNOWN)), gr.Thresholds())]
    assert labels[0] == "<= -9" and labels[5] == "-1 to +1" and labels[-1] == ">= +9"


def test_a_stop_creates_no_piece() -> None:
    dist, alt = profile(KNOWN)
    # standing at 1550 m (mid-piece) for 120 samples, the barometer drifting by 2 m meanwhile
    k = int(np.searchsorted(dist, 1550))
    still_d = np.full(120, dist[k])
    still_a = alt[k] + np.linspace(0, 2, 120)
    d2 = np.r_[dist[:k], still_d, dist[k + 1 :]]
    a2 = np.r_[alt[:k], still_a, alt[k + 1 :] + 2]
    plain, stopped = gr.cut(dist, alt), gr.cut(d2, a2)
    assert len(stopped) == len(plain) == 35
    np.testing.assert_allclose(stopped.edges_m, plain.edges_m)
    # the altitude at 1600 m is the one where the distance first reached it: the drift during
    # the stop shifts the level, not the number of pieces
    assert list(stopped.grade_pct[:15]) == list(plain.grade_pct[:15])


def test_stretch_from_to_and_the_last_piece() -> None:
    c = course(KNOWN)
    th = gr.Thresholds()
    part = gr.measure(c, th, from_km=0.5, to_km=2.6)
    assert part["pieces"] == 21 and part["stretch"] == {
        "from_km": 0.5,
        "to_km": 2.6,
        "whole": False,
    }
    assert by_band(part)["steep_up"] == 1.9 and by_band(part)["gentle_up"] == 0.2
    assert by_band(part)["flat"] == 0.0 and part["ascent_m"] == 100
    assert gr.measure(c, th, to_km=3.44)["pieces"] == 34  # a last 40 m piece is dropped
    longer = gr.measure(c, th, to_km=3.46)
    assert longer["pieces"] == 35 and longer["stretch"]["to_km"] == 3.46  # 60 m is kept
    beyond = gr.measure(c, th, from_km=3.0, to_km=9.0)
    assert beyond["stretch"]["to_km"] == 3.5 and "cut to the course's end" in beyond["notes"][0]
    with pytest.raises(AnalysisError, match=r"course ends at km 3\.50"):
        gr.measure(c, th, from_km=4.0)
    with pytest.raises(AnalysisError, match="shorter than half a piece"):
        gr.measure(c, th, from_km=3.48)


def test_course_specs() -> None:
    assert gr.parse_spec("602@3-20.1") == ("602", 3.0, 20.1)
    assert gr.parse_spec(" race.gpx@-5 ") == ("race.gpx", None, 5.0)
    assert gr.parse_spec("last@5-") == ("last", 5.0, None)
    assert gr.parse_spec("2030-03-04") == ("2030-03-04", None, None)
    assert gr.parse_spec("inbox/a@b.gpx") == ("inbox/a@b.gpx", None, None)
    with pytest.raises(AnalysisError, match="end after it starts"):
        gr.parse_spec("602@5-3")


def test_smoothing_by_distance_keeps_slopes_and_removes_spikes() -> None:
    dist = np.arange(0.0, 1001.0, 25.0)  # unevenly spaced against the 10 m grid
    ele = 50 + dist * 0.04
    at, smooth = gr.smooth_by_distance(dist, ele)
    assert at[1] - at[0] == 10.0 and at[-1] == 1000.0
    np.testing.assert_allclose(smooth, 50 + at * 0.04)  # straight to both ends
    spiky = ele.copy()
    spiky[20] += 7.0  # a bridge in the terrain model
    _, smooth = gr.smooth_by_distance(dist, spiky)
    assert np.max(smooth - (50 + at * 0.04)) < 3.5


def test_short_dips_do_not_split_a_climb() -> None:
    th = gr.Thresholds()
    small = gr.measure(course([(1000, 3.0), (200, -2.0), (1000, 3.0)]), th)  # 4 m dip
    assert [(c["from_km"], c["to_km"], c["gain_m"]) for c in small["climbs"]] == [(0.0, 2.2, 56.0)]
    deep = gr.measure(course([(1000, 3.0), (300, -3.0), (1000, 3.0)]), th)  # 9 m dip
    assert [(c["from_km"], c["gain_m"]) for c in deep["climbs"]] == [(0.0, 30.0), (1.3, 30.0)]


def test_likeness_of_identical_and_opposite_courses() -> None:
    same = gr.compare([course(KNOWN, "a"), course(KNOWN, "b")])
    assert same["likeness"] == [{"a": "A", "b": "B", "overlap_pct": 100.0, "words": "very alike"}]
    opposite = gr.compare([course([(2000, 4.0)], "up"), course([(2000, -4.0)], "down")])
    assert opposite["likeness"][0]["overlap_pct"] == 0.0
    assert opposite["likeness"][0]["words"] == "different"
    assert opposite["comparison"][0].startswith("down against up: less climbing per km (0 vs 40 m)")


def test_ramps_against_a_long_gentle_climb() -> None:
    ramps = []
    for _ in range(4):  # 300 m at 7 % (a short steep ramp), down again, flat
        ramps += [(700, 0.0), (300, 7.0), (300, -7.0)]
    long_climb = [(1000, 0.0), (2500, 3.0), (1700, -4.4)]
    result = gr.compare([course(ramps, "Race"), course(long_climb, "Home")])
    race, home = result["courses"]
    assert race["short_ramps"] == 4 and home["short_ramps"] == 0
    assert (race["short_ramps_km"], race["short_ramps_grade_pct"]) == (1.2, 7.0)
    assert (home["short_ramps_km"], home["short_ramps_grade_pct"]) == (0.0, None)
    assert race["climb_kind"] == "short_steep" and home["climb_kind"] == "long_gentle"
    assert (
        "4 short steep ramps (each under 0.5 km): 1.2 km in all at 7.0 % on average (7.0 % "
        "each), at km 0.7, 2.0, 3.3, 4.6;" in race["summary"]
    )
    assert "all of it gentle (1-5 %); mostly long gentle climbs (longest 2.5 km" in home["summary"]
    (line,) = result["comparison"]
    assert line.startswith("Home against Race: ")
    assert (
        "less steep uphill (0.0 vs 1.2 km, 0 vs 23 % of the distance), in short steep ramps none"
        " vs 1.2 km at 7.0 %;" in line
    )
    assert "Home has mostly long gentle climbs, Race has mostly short steep climbs" in line


def test_many_tiny_ramps_do_not_read_as_more_steep_running() -> None:
    """Six 400 m ramps at 9 % against nine single steep pieces or pairs at 5.5 to 6 %: the
    second course has more ramps but less steep running, and the words must say the latter."""
    long_ramps = [(600, 0.0), (400, 9.0), (400, -9.0)] * 6  # 2.4 km steep of 8.4 km
    tiny = [(100, 6.0), (300, -2.0), (400, 0.0)] * 5  # 5 x 0.1 km at 6 %
    tiny += [(200, 5.5), (500, -2.2), (300, 0.0)] * 4  # 4 x 0.2 km at 5.5 %: 1.3 km of 8 km
    result = gr.compare([course(long_ramps, "A"), course(tiny, "B")])
    a, b = result["courses"]
    assert (a["short_ramps"], a["short_ramps_km"], a["short_ramps_grade_pct"]) == (6, 2.4, 9.0)
    assert (b["short_ramps"], b["short_ramps_km"], b["short_ramps_grade_pct"]) == (9, 1.3, 5.7)
    assert (
        "6 short steep ramps (each under 0.5 km): 2.4 km in all at 9.0 % on average (9.0 % "
        "each)" in a["summary"]
    )
    assert (
        "9 short steep ramps (each under 0.5 km): 1.3 km in all at 5.7 % on average (5.5 to "
        "6.0 % each)" in b["summary"]
    )
    (line,) = result["comparison"]
    assert (
        "less steep uphill (1.3 vs 2.4 km, 16 vs 29 % of the distance), in short steep ramps "
        "1.3 km at 5.7 % vs 2.4 km at 9.0 %;" in line
    )
    assert "9 vs 6" not in line and "more steep" not in line


def test_steep_uphill_in_km_and_as_a_share_can_disagree() -> None:
    """A short course with less steep running in total but more of its distance steep."""
    result = gr.compare(
        [course([(1000, 6.0), (9000, 0.0)], "long"), course([(600, 6.0), (2400, 0.0)], "short")]
    )
    (line,) = result["comparison"]
    assert (
        "short against long: more climbing per km (12 vs 6 m), less steep uphill in total (0.6 vs"
        " 1.0 km) but more as a share of the distance (20 vs 10 %);" in line
    )
    same_km = gr.compare(
        [course([(1000, 6.0), (9000, 0.0)], "x"), course([(1000, 6.0), (4000, 0.0)], "y")]
    )
    assert (
        "about as much steep uphill in total (1.0 vs 1.0 km) but more as a share"
        in (same_km["comparison"][0])
    )


def test_words_follow_the_thresholds() -> None:
    assert [gr.hill_word(v) for v in (0, 4.9, 5, 12, 25)] == [
        "flat",
        "flat",
        "gently rolling",
        "hilly",
        "very hilly",
    ]
    assert [gr.like_word(v) for v in (0, 49.9, 50, 70, 85, 100)] == [
        "different",
        "different",
        "partly alike",
        "alike",
        "very alike",
        "very alike",
    ]
    flat = gr.measure(course([(3000, 0.4)]), gr.Thresholds())
    assert "no piece climbs 1 % or more; no climb of 10 m or more" in flat["summary"]
    a = gr.measure(course([(1000, 1.0)]), gr.Thresholds(min_climb_m=5))
    b = gr.measure(course([(1000, 1.2)]), gr.Thresholds(min_climb_m=5))
    assert "about as much climbing per km (12 vs 10 m)" in gr.comparison_words(a, b, 50.0)


def test_the_words_and_smoothing_of_coach_route() -> None:
    from thresy.tools import route_report as rr

    assert gr.HILL_LEVELS_M_PER_KM == rr.HILL_LEVELS_M_PER_KM
    assert gr.HILL_WORDS == rr.HILL_WORDS
    assert (gr.FILE_STEP_M, gr.FILE_SMOOTH_M) == (rr.STEP_M, rr.ELE_SMOOTH_M)


def test_the_analysis_package_imports_no_tools() -> None:
    """thresy.tools uses thresy.analysis; the other way round only at call time (GPX files)."""
    import subprocess
    import sys

    code = (
        "import sys, thresy.analysis.grades, thresy.analysis.cli; "
        "print([m for m in sys.modules if m.startswith('thresy.tools')])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_thresholds_are_checked() -> None:
    with pytest.raises(AnalysisError, match="flat-pct"):
        gr.compare([course(KNOWN)], gr.Thresholds(flat_pct=6))
    with pytest.raises(AnalysisError, match="1 to 4 courses"):
        gr.compare([course(KNOWN)] * 5)


# The command with files (no database) -----------------------------------------------------------


@pytest.fixture
def ws(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("COACH_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("COACH_LANGUAGE", "en")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def drawn(monkeypatch) -> list:
    """The figures the pictures were drawn on (saved as usual)."""
    from thresy.analysis import plots

    figures: list = []
    save = plots._save

    def keep(fig, name, out_dir):
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        path = save(fig, name, out_dir)
        FigureCanvasAgg(fig)
        figures.append(fig)
        return path

    monkeypatch.setattr(plots, "_save", keep)
    return figures


def run(*args: str):
    return runner.invoke(app, ["grades", *args])


def as_json(*args: str) -> dict:
    result = run(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_a_gpx_course(ws) -> None:
    path = gpx(ws / "known.gpx", KNOWN)
    out = as_json(str(path))
    (r,) = out["courses"]
    check_known(r)
    assert r["kind"] == "file" and r["elevation_source"] == "file" and r["label"] == "known.gpx"
    assert r["file"] == str(path) and r["track_name"] == "known"
    assert r["recorded_ascent_m"] is None and "_pieces" not in r
    assert len(r["piece_grade_pct"]) == 35 and r["piece_from_km"][6] == 0.6
    assert out["settings"]["bin_m"] == 100 and out["notes"] == []
    text = run(str(path)).output
    assert "elevation from the file (smoothed over 70 m)" in text
    assert "very hilly (29 m up per km" in text and "ascent m (watch" not in text


def test_a_gpx_course_relative_to_the_workspace(ws) -> None:
    (ws / "ws" / "inbox").mkdir(parents=True)
    gpx(ws / "ws" / "inbox" / "race.gpx", KNOWN)
    assert as_json("inbox/race.gpx@0.5-2.6")["courses"][0]["pieces"] == 21


def test_a_gpx_course_without_elevation_is_refused(ws) -> None:
    path = gpx(ws / "flat.gpx", KNOWN, elevation=False)
    result = run(str(path))
    assert result.exit_code == 1 and "flat.gpx has no elevation" in result.output
    missing = run(str(ws / "nope.gpx"))
    assert missing.exit_code == 1 and "no such file" in missing.output


def test_a_gpx_course_with_gaps_in_its_elevation(ws) -> None:
    path = gpx(ws / "gaps.gpx", KNOWN)
    text = path.read_text()
    first = text.index("<ele>")
    path.write_text(text[:first] + text[text.index("</ele>", first) + 6 :])  # one point
    (r,) = as_json(str(path))["courses"]
    assert r["points_without_elevation"] == 1 and r["points"] == 351
    check_known(r)


def test_four_courses_labels_and_five_refused(ws) -> None:
    files = [str(gpx(ws / f"c{k}.gpx", KNOWN)) for k in range(5)]
    out = as_json(*files[:4], "--labels", "Race,,Home")
    assert [r["label"] for r in out["courses"]] == ["Race", "c1.gpx", "Home", "c3.gpx"]
    assert [r["key"] for r in out["courses"]] == ["A", "B", "C", "D"]
    assert len(out["likeness"]) == 6 and {x["overlap_pct"] for x in out["likeness"]} == {100.0}
    assert len(out["comparison"]) == 3
    assert out["comparison"][1].startswith("Home against Race: about as much climbing")
    five = run(*files)
    assert five.exit_code == 2 and "at most 4 courses, got 5" in five.output
    labels = run(*files[:2], "--labels", "a,b,c")
    assert labels.exit_code == 2 and "3 labels for 2 courses" in labels.output
    bad = run(files[0], "--flat-pct", "5", "--steep-pct", "5")
    assert bad.exit_code == 2 and "--flat-pct" in bad.output


def test_text_of_two_courses(ws) -> None:
    race = gpx(ws / "race.gpx", [(700, 0.0), (300, 7.0), (300, -7.0)] * 3)
    home = gpx(ws / "home.gpx", [(1000, 0.0), (2500, 3.0), (1700, -4.4)])
    text = run(str(race), str(home), "--labels", "Race,Home").output
    lines = text.splitlines()
    assert lines[1].startswith("A = Race: file ") and lines[2].startswith("B = Home: file ")
    words = lines.index("In words (from the numbers below):")
    assert lines[words + 1].startswith("A Race: hilly (15 m up per km")
    assert lines[words + 3].startswith("Home against Race: ")
    for heading in ("Bands (km, and % of", "Gradient classes, 2 % wide", "Climbs (at least 10 m"):
        assert any(line.startswith(heading) for line in lines), heading
    assert "Likeness of the gradient classes (overlap; 100 % = the same mix): A-B" in text


@pytest.mark.parametrize("lang", ["en", "de"])
def test_plot_in_both_languages(ws, drawn, monkeypatch, lang) -> None:
    monkeypatch.setenv("COACH_LANGUAGE", lang)
    get_settings.cache_clear()
    race = gpx(ws / "race.gpx", [(700, 0.0), (300, 7.0), (300, -7.0)] * 3)
    home = gpx(ws / "home.gpx", [(1000, 0.0), (2500, 3.0), (1700, -4.4)])
    out = as_json(str(race), f"{home}@1-", "--plot", "--share")
    path = Path(out["plot"])
    assert path == ws / "ws" / "out" / "grades-race_home@1-5.2.png" and path.is_file()
    fig = drawn[-1]
    title = fig._suptitle.get_text()
    assert title == ("Steigung je 100 m" if lang == "de" else "Gradient per 100 m")
    panels, classes = fig.axes[:2], fig.axes[2]
    assert [ax.get_title(loc="left") for ax in panels] == ["race.gpx", "home.gpx"]
    assert all(ax.get_ylim() == panels[0].get_ylim() for ax in panels)  # same gradient scale
    spans = [ax.get_xlim()[1] - ax.get_xlim()[0] for ax in panels]
    assert spans[0] == pytest.approx(spans[1])  # same km per inch
    assert panels[1].get_xlim()[0] == pytest.approx(1.0)  # course km of the stretch
    share = "Anteil der Strecke [%]" if lang == "de" else "Share of the course [%]"
    assert classes.get_ylabel() == share
    assert [t.get_text() for t in classes.get_legend().get_texts()] == ["race.gpx", "home.gpx"]
    from PIL import Image

    with Image.open(path) as img:
        assert img.width <= 6.4 * 110 + 2  # phone width, nothing sticking out


# The command with activities --------------------------------------------------------------------


def known_phases(*, stop: bool = False) -> list[Phase]:
    """:data:`KNOWN` at 2.5 m/s; with ``stop`` a 60 s stand and a 2 min watch pause at 1550 m."""
    climb = [Phase(800, speed=2.5, grade=0.05)]
    if stop:
        climb = [
            Phase(400, speed=2.5, grade=0.05),
            Phase(60, speed=0.0, hr=110),
            Phase(120, paused=True),
            Phase(400, speed=2.5, grade=0.05),
        ]
    return [Phase(220, speed=2.5), *climb, Phase(380, speed=2.5)]


@pytest.fixture
def db_env(monkeypatch, ws, test_db, conn):
    monkeypatch.setenv("COACH_DATABASE_URL", test_db.app_url)
    get_settings.cache_clear()
    put_activity(conn, 701, records(known_phases()), name="Hill Run")
    later = T0.replace(day=T0.day + 1)
    put_activity(conn, 702, records(known_phases(stop=True), t0=later), t0=later, name="Stops")
    return conn


@pytest.mark.db
def test_activities_with_and_without_stops(db_env, ws) -> None:
    out = as_json("701", "last")
    plain, stopped = out["courses"]
    check_known(plain)
    check_known(stopped)  # a stand and a watch pause create no piece and change no gradient
    assert plain["label"] == "2030-03-04 Hill Run" and plain["activity_id"] == 701
    assert stopped["activity_id"] == 702 and stopped["selected"] == "last run"
    assert plain["elevation_source"] == "watch" and plain["recorded_ascent_m"] == 100
    assert out["likeness"][0]["overlap_pct"] == 100.0
    text = run("701", "2030-03-05").output
    assert "ascent m (watch, all)" in text and "the watch's recording (smoothed over 15 s)" in text


@pytest.mark.db
def test_an_activity_stretch_against_a_gpx_course(db_env, ws) -> None:
    path = gpx(ws / "race.gpx", KNOWN)
    out = as_json(f"{path}@0.5-2.6", "701@0.5-2.6", "--labels", "Race,Run", "--plot")
    race, run_ = out["courses"]
    assert race["pieces"] == run_["pieces"] == 21
    assert by_band(race) == by_band(run_)
    assert out["likeness"][0]["overlap_pct"] == 100.0
    assert out["notes"] and out["notes"][-1].startswith(
        "elevation measured differently: A from a file, B from a watch"
    )
    assert Path(out["plot"]).name == "grades-race@0.5-2.6_701@0.5-2.6.png"
    missing = run("999")
    assert missing.exit_code == 1 and "no activity with id 999" in missing.output
