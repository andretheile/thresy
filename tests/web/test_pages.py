"""The pages, charts, texts and the static demo, without a database."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from markupsafe import escape
from typer.testing import CliRunner

from thresy.cli import get_app
from thresy.web import charts, pages, text
from thresy.web.demo import DEMO_NOW, DemoSource, build
from thresy.web.export import export_pages, single_page
from thresy.web.i18n import TEXTS, Lang
from thresy.web.render import Renderer
from thresy.web.security import parse_password_hash, verify_password

EN, DE = Lang.get("en"), Lang.get("de")


def esc(text: str) -> str:
    """A text as it appears in the HTML."""
    return str(escape(text))


@pytest.fixture(scope="module")
def full() -> DemoSource:
    return DemoSource("full", "en")


@pytest.fixture(scope="module")
def new() -> DemoSource:
    return DemoSource("new", "en")


def render_all(source: DemoSource, lang: Lang) -> dict[str, str]:
    r = Renderer(lang)
    run = next(a for a in source.runs() if a.has_records)
    out = {
        "today": r.render("overview.html", pages.overview_page(source, lang, coach_name="Thresy")),
        "runs": r.render("runs.html", pages.runs_page(source, lang, None)),
        "run": r.render("run.html", pages.run_page(source, lang, run.id)),
        "trends": r.render("trends.html", pages.trends_page(source, lang)),
        "races": r.render("races.html", pages.races_page(source, lang)),
        "segments": r.render("segments.html", pages.segments_page(source, lang)),
        "data": r.render("data.html", pages.data_page(source, lang)),
    }
    old = next((a for a in source.runs() if not a.has_records), None)
    if old is not None:
        out["old_run"] = r.render("run.html", pages.run_page(source, lang, old.id))
    return out


# -- the demo athlete ------------------------------------------------------------------------------


def test_demo_is_deterministic() -> None:
    build.cache_clear()
    a = build("full", "en")
    build.cache_clear()
    b = build("full", "en")
    assert [x["distance_m"] for x in a.activities] == [x["distance_m"] for x in b.activities]
    assert a.wellness == b.wellness


def test_demo_is_plausible(full: DemoSource) -> None:
    data = full.data
    runs = [a for a in data.activities if a["is_run"]]
    assert len(runs) > 300
    assert all(240 < (a["moving_s"] / (a["distance_m"] / 1000)) < 420 for a in runs)
    assert len(data.races) == 2 and len(data.segments) == 2
    assert full.night(full.today())["synced"]
    week = full.plan_week(full.today() - timedelta(days=full.today().weekday()), 1, full.today())
    assert week["totals"]["planned"] >= 4
    assert full.coach_message()["trigger"] == "morning_briefing"


def test_demo_records_match_the_summary(full: DemoSource) -> None:
    run = next(a for a in full.runs() if a.has_records)
    df = full.records(run.id)
    assert abs(df["dist"].iloc[-1] - run.distance_m) < 1
    assert abs(df["hr"].mean() - run.avg_hr) < 1


# -- pages -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", [EN, DE], ids=["en", "de"])
def test_every_page_renders(full: DemoSource, lang: Lang) -> None:
    html = render_all(full, lang)
    assert "Riverside Half Marathon" in html["today"] or "Halbmarathon" in html["today"]
    assert "<svg" in html["run"] and "profile-data" in html["run"]
    assert esc(lang.t("no_records")) in html["old_run"]
    for name, page in html.items():
        assert "Traceback" not in page, name
        assert page.count("<html") == 1, name


def test_overview_content(full: DemoSource) -> None:
    ctx = pages.overview_page(full, EN, coach_name="Coach")
    night = ctx["night"]
    assert night["state"] == "synced"
    assert night["hrv"]["state"] in ("in", "below", "above")
    assert ctx["today_plan"] and ctx["today_plan"][0]["status"] == "planned"
    assert ctx["coach"]["who"] == "Coach said"
    assert ctx["race"]["days"] == (date(2030, 5, 26) - full.today()).days
    assert len(ctx["recent"]) == 5
    assert ctx["notices"] == []


def test_new_athlete_pages_look_intended(new: DemoSource) -> None:
    html = render_all(new, EN)
    assert esc(EN.t("night_pending_title")) in html["today"]
    assert esc(EN.t("today_no_plan")) in html["today"]
    assert esc(EN.t("coach_none")) in html["today"]
    assert esc(EN.t("races_none")) in html["races"]
    assert esc(EN.t("segments_none")) in html["segments"]
    assert esc(EN.t("trends_empty")) in html["trends"]  # too few nights for the recovery trends
    ctx = pages.overview_page(new, EN, coach_name="Thresy")
    assert ctx["volume"]["avg4"] == ""  # no four-week average before four weeks of running
    assert len(pages.trim_weeks(new.weeks(new.today(), 26)["weeks"])) == pages.MIN_WEEKS_SHOWN


def test_trouble_is_shown_calmly_and_honestly() -> None:
    source = DemoSource("trouble", "en")
    ctx = pages.overview_page(source, EN, coach_name="Thresy")
    titles = [n["title"] for n in ctx["notices"]]
    assert titles == [EN.t("notice_auth_title"), EN.t("notice_backup_title")]
    assert ctx["night"]["state"] == "pending" and ctx["night"]["blocked"]
    assert not ctx["sync"]["ok"]
    html = Renderer(EN).render("overview.html", ctx)
    assert esc(EN.t("night_blocked_text")) in html
    assert "sync-pill is-off" in html
    data = Renderer(EN).render("data.html", pages.data_page(source, EN))
    assert "quota is exceeded" in data
    assert pages.overview_page(DemoSource("full", "en"), EN, coach_name="T")["sync"]["ok"]


def test_excerpt_keeps_words_and_drops_markdown() -> None:
    short, more = text.excerpt(
        "**Today:** easy `5 km`, [details](https://x.org) " + "word " * 100, 40
    )
    assert more and short.endswith("…")
    assert not any(mark in short for mark in ("**", "`", "[", "]("))
    assert short.startswith("Today: easy 5 km, details")
    assert text.excerpt("short", 40) == ("short", False)


DEBRIEF_WITH_TABLE = """Nice and **easy** yesterday. The blocks were clean:

| Block | Pace | HR |
|---|---:|---:|
| 1 | 3:56 | 164 |
| 2 | 3:58 | 166 |

Sunday is the key session."""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (DEBRIEF_WITH_TABLE, "Nice and easy yesterday. The blocks were clean…"),
        ("Two things:\n\n- sleep\n- eat\n\nThat is all.", "Two things…"),
        ("Look:\n\n```\ncode\n```", "Look…"),
        ("Intro.\n\n> a quote", "Intro."),
        ("| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter the table.", "After the table."),
        ("- only\n- a list", "only · a list"),
    ],
)
def test_excerpt_is_prose_only(source: str, expected: str) -> None:
    short, more = text.excerpt(source)
    assert short == expected
    assert more
    assert "|" not in short


def test_tables_are_rendered_without_inline_styles() -> None:
    html = text.render_markdown(DEBRIEF_WITH_TABLE)
    assert '<div class="md-table"><table>' in html
    assert '<th class="al-right">Pace</th>' in html
    assert "<td>1</td>" in html and "style=" not in html
    assert "<strong>easy</strong>" in html


def test_markdown_is_safe() -> None:
    html = text.render_markdown(
        "<script>alert(1)</script> [x](https://example.org) ![i](https://example.org/i.png)"
        " [y](javascript:alert(1))"
    )
    assert "<script>" not in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert "<img" not in html
    assert "javascript:" not in html.replace("[y](javascript:alert(1))", "")


def test_a_debrief_with_a_table_is_shortened_to_prose() -> None:
    source = DemoSource("trouble", "en")
    assert "| Block |" in source.coach_message()["text"]
    ctx = pages.overview_page(source, EN, coach_name="Thresy")
    assert ctx["coach"]["more"] and "|" not in ctx["coach"]["short"]
    html = Renderer(EN).render("overview.html", ctx)
    assert '<details class="expand quote">' in html
    assert '<div class="md-table"><table>' in html
    assert not re.search(r"\sstyle=", html)


def test_long_plan_descriptions_open_on_request() -> None:
    source = DemoSource("full", "en")
    ctx = pages.overview_page(source, EN, coach_name="Thresy")
    session = ctx["today_plan"][0]
    assert len(session["description"]) > 800
    assert session["desc_more"] and len(session["desc_short"]) <= pages.DESCRIPTION_CHARS + 1
    html = Renderer(EN).render("overview.html", ctx)
    assert '<details class="expand desc">' in html
    # on the page of the run that fulfilled a session with a long description
    monday = source.today() - timedelta(days=source.today().weekday())
    run = next(
        a for a in source.runs(since=monday) if source.planned_for(a.id)[0]["kind"] == "threshold"
    )
    run_ctx = pages.run_page(source, EN, run.id)
    assert run_ctx["plan"][0]["desc_more"]
    assert '<details class="expand desc">' in Renderer(EN).render("run.html", run_ctx)
    # short ones stay as they are
    short = next(w for w in source.data.workouts if len(w["description"]) < 100)
    assert not pages._workout(short, EN, source.today())["desc_more"]


class StateSource(DemoSource):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__("full", "en")
        self._state = state

    def state(self) -> dict[str, Any]:
        return {
            "sync_status": {},
            "garmin_auth": {},
            "garmin_rate_limit": {},
            "backup_status": {},
            **self._state,
        }


def iso(delta: timedelta) -> str:
    return (DEMO_NOW - delta).isoformat()


@pytest.mark.parametrize(
    ("state", "titles"),
    [
        ({"garmin_auth": {"status": "login_failed"}}, ["notice_auth_title"]),
        (
            {
                "sync_status": {
                    "last_success_at": iso(timedelta(hours=20)),
                    "consecutive_failures": 4,
                }
            },
            ["notice_sync_title"],
        ),
        (
            {
                "sync_status": {
                    "last_success_at": iso(timedelta(hours=1)),
                    "consecutive_failures": 1,
                }
            },
            [],
        ),
        (
            {"garmin_rate_limit": {"until": (DEMO_NOW + timedelta(hours=1)).isoformat()}},
            ["notice_rate_title"],
        ),
        ({"backup_status": {"last_success_at": iso(timedelta(days=9))}}, ["notice_backup_title"]),
        ({"backup_status": {"last_success_at": iso(timedelta(days=1))}}, []),
        (
            {"backup_status": {"last_success_at": iso(timedelta(days=1)), "failures_in_row": 2}},
            ["notice_backup_title"],
        ),
        ({}, []),
    ],
)
def test_notices(state: dict[str, Any], titles: list[str]) -> None:
    found = pages.notices(StateSource(state), EN)
    assert [n["title"] for n in found] == [EN.t(t) for t in titles]


def test_data_page_redacts_stored_errors() -> None:
    source = StateSource(
        {
            "sync_status": {
                "consecutive_failures": 2,
                "last_error": "login to postgresql://u:hunter2@h/db failed",
            }
        }
    )
    html = Renderer(EN).render("data.html", pages.data_page(source, EN))
    assert "hunter2" not in html


# -- texts and formats -----------------------------------------------------------------------------


def test_catalogues_have_the_same_keys() -> None:
    assert set(TEXTS["en"]) == set(TEXTS["de"])


def test_templates_use_known_texts() -> None:
    templates = Path(pages.__file__).parent / "templates"
    keys = set()
    for path in templates.glob("*.html"):
        keys |= set(re.findall(r"""L\.tn?\(["']([a-z0-9_.]+)["']""", path.read_text()))
    keys = {k for k in keys if not k.endswith("_")}  # prefixes of computed keys ("nav_" + key)
    assert keys - set(TEXTS["en"]) == set()
    for key in ("today", "runs", "trends", "races", "segments", "data"):
        assert f"nav_{key}" in TEXTS["en"]


def test_number_and_date_formats() -> None:
    assert EN.num(1234.5, 1) == "1,234.5"
    assert DE.num(1234.5, 1) == "1.234,5"
    assert EN.num(-3, 0) == "−3"
    assert EN.pace(275.4) == "4:35"
    assert EN.clock(3725) == "1:02:05"
    assert EN.clock(75) == "1:15"
    assert EN.hm(7 * 3600 + 32 * 60) == "7 h 32 min"
    day = date(2030, 4, 18)
    assert EN.date(day, today=day) == "Thu 18 Apr"
    assert DE.date(day, today=day) == "Do 18. Apr"
    assert EN.date(day, today=date(2031, 1, 1)) == "Thu 18 Apr 2030"
    assert DE.date_long(day) == "Donnerstag, 18. April"
    assert EN.tn("days_to_go", 1) == "day to go"
    assert DE.tn("days_to_go", 38) == "Tage bis zum Start"
    now = datetime(2030, 4, 18, 8, 0, tzinfo=UTC)
    assert EN.ago(now - timedelta(minutes=4), now) == "4 min ago"
    assert DE.ago(now - timedelta(minutes=4), now) == "vor 4 Min."
    assert Lang.get("fr").code == "en"


# -- charts ----------------------------------------------------------------------------------------


def test_ticks() -> None:
    assert charts.nice_ticks(0, 61, 3) == [0, 25, 50, 75]
    assert charts.time_ticks(245, 410, 4) == [240, 300, 360, 420]


def test_charts_are_well_formed_svg(full: DemoSource) -> None:
    html = render_all(full, EN)
    count = 0
    for page in html.values():
        for svg in re.findall(r"<svg\b.*?</svg>", page, flags=re.S):
            ET.fromstring(svg.replace("&nbsp;", " "))
            count += 1
    assert count > 40


def test_chart_text_is_escaped() -> None:
    svg = charts.Svg(100, 20, label='<b>"x"</b>')
    svg.text(0, 10, "<script>")
    svg.hit(0, 0, 10, 10, '"><script>')
    out = str(svg.render())
    assert "<script>" not in out and "<b>" not in out
    ET.fromstring(out)


# -- export and the CLI ----------------------------------------------------------------------------


def test_export_pages_link_to_each_other(tmp_path: Path) -> None:
    written = export_pages(tmp_path, lang="en", scenario="new")
    assert (tmp_path / "index.html") in written
    for path in tmp_path.glob("*.html"):
        for ref in re.findall(r'(?:href|src)="([^"#?]+)', path.read_text()):
            assert not ref.startswith(("http:", "https:", "//", "/")), (path.name, ref)
            assert (tmp_path / ref).exists(), (path.name, ref)


def test_single_file_is_self_contained() -> None:
    html = single_page(lang="en")
    assert "<style>" in html and "Riverside Half Marathon" in html
    assert "An invented athlete" in html
    refs = re.findall(r'(?:href|src|action)="([^"]*)"', html)
    assert all(r.startswith(("data:", "#")) for r in refs), refs
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in html
    assert '<link rel="stylesheet"' not in html


def test_cli_demo(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        get_app(), ["web", "demo", "--out", str(tmp_path / "one.html"), "--single-file"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "one.html").read_text().startswith("<!doctype html>")
    result = runner.invoke(
        get_app(),
        ["web", "demo", "--out", str(tmp_path / "site"), "--scenario", "new", "--language", "de"],
    )
    assert result.exit_code == 0, result.output
    assert "Noch nicht synchronisiert" in (tmp_path / "site" / "index.html").read_text()


def test_cli_password() -> None:
    runner = CliRunner()
    result = runner.invoke(get_app(), ["web", "password"])
    assert result.exit_code == 0
    password = re.search(r"^\s+([a-z0-9-]{29})$", result.output, flags=re.M).group(1)
    stored = re.search(r"COACH_WEB_PASSWORD_HASH=(\S+)", result.output).group(1)
    assert verify_password(password, parse_password_hash(stored))
    result = runner.invoke(
        get_app(), ["web", "password", "--stdin"], input="my own long passphrase\n"
    )
    assert result.exit_code == 0
    stored = result.output.strip().split("=", 1)[1]
    assert verify_password("my own long passphrase", parse_password_hash(stored))
    assert "my own long passphrase" not in result.output
    short = runner.invoke(get_app(), ["web", "password", "--stdin"], input="short\n")
    assert short.exit_code != 0
