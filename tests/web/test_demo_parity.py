"""The database path of the dashboard against the demo path.

The demo athlete is loaded into the test database; :class:`thresy.web.source.DbSource` (the
read-only role, through the ``coach`` library functions) must then answer like
:class:`thresy.web.demo.DemoSource`, and the rendered pages must be the same. This keeps the
demo honest and checks the database queries of the dashboard against realistic data.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import numpy as np
import pytest

from thresy import db
from thresy.web import pages
from thresy.web.demo import DEMO_NOW, DEMO_ZONE, DemoSource
from thresy.web.i18n import Lang
from thresy.web.render import Renderer
from thresy.web.source import DbSource

from . import demo_db

pytestmark = pytest.mark.db

EN = Lang.get("en")


@pytest.fixture(scope="module")
def loaded(test_db: Any) -> Any:
    """The full demo athlete in the session's test database."""
    with db.connect(test_db.app_url) as conn:
        tables = [
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                " AND tablename <> 'schema_migrations'"
            ).fetchall()
        ]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        demo_db.load(conn, "full", "en")
    yield test_db
    with db.connect(test_db.app_url) as conn:
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")


@pytest.fixture
def sources(loaded: Any) -> Any:
    demo = DemoSource("full", "en")
    with db.connect_ro(loaded.ro_url) as conn:
        yield DbSource(conn, DEMO_ZONE, now=DEMO_NOW), demo


def normalise(value: Any) -> Any:
    """Floats rounded (the database stores some columns as ``real``)."""
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalise(v) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    return value


def test_weeks_and_years(sources: Any) -> None:
    dbs, demo = sources
    today = demo.today()
    assert normalise(dbs.weeks(today, 26)) == normalise(demo.weeks(today, 26))
    assert normalise(dbs.run_years()) == normalise(demo.run_years())
    since = today - timedelta(days=500)
    assert normalise(dbs.daily_distance(since, today)) == normalise(
        demo.daily_distance(since, today)
    )


def test_wellness_and_nights(sources: Any) -> None:
    dbs, demo = sources
    today = demo.today()
    assert normalise(dbs.wellness(today, 84)) == normalise(demo.wellness(today, 84))
    for day in (today, today - timedelta(days=30)):
        a, b = dbs.night(day), demo.night(day)
        for key in ("synced", "sleep_synced_at", "sleep", "garmin_hrv", "resting_hr", "levels"):
            assert normalise(a[key]) == normalise(b[key]), key
    missing = today + timedelta(days=1)
    assert dbs.night(missing)["latest_synced"] == demo.night(missing)["latest_synced"] == today


def test_plan_races_runs_and_messages(sources: Any) -> None:
    dbs, demo = sources
    today = demo.today()
    monday = today - timedelta(days=today.weekday())
    for start in (monday, monday - timedelta(days=14)):
        a, b = dbs.plan_week(start, 2, today), demo.plan_week(start, 2, today)
        for day_a, day_b in zip(a["days"], b["days"], strict=True):
            for w in day_a["workouts"] + day_b["workouts"]:
                w.pop("created_at", None), w.pop("updated_at", None)
        assert normalise(a) == normalise(b)
    assert normalise(dbs.races(today)) == normalise(demo.races(today))
    assert dbs.runs(limit=20) == demo.runs(limit=20)
    assert dbs.coach_message()["text"] == demo.coach_message()["text"]
    assert normalise(dbs.state()) == normalise(demo.state())


def test_segments(sources: Any) -> None:
    dbs, demo = sources
    assert dbs.segments() == demo.segments()
    for seg in demo.segments():
        a, b = dbs.segment_history(seg), demo.segment_history(seg)
        assert a["count"] == b["count"]
        assert a["best"]["duration_s"] == pytest.approx(b["best"]["duration_s"], abs=0.01)
        assert a["efforts"][-1]["start_local"] == b["efforts"][-1]["start_local"]
    run = demo.segment_history(demo.segments()[0])["efforts"][-1]["activity_id"]
    assert len(dbs.activity_efforts(run)) == len(demo.activity_efforts(run))


def test_records_of_one_run(sources: Any) -> None:
    dbs, demo = sources
    run = next(a for a in dbs.runs() if a.has_records)
    a, b = dbs.records(run.id), demo.records(run.id)
    assert len(a) == len(b)
    for col in ("t", "dist", "speed", "hr", "alt", "lat", "lon", "timer"):
        assert np.allclose(a[col].to_numpy(float), b[col].to_numpy(float), atol=0.05), col
    first = a["ts"].iloc[0]
    assert len(dbs.laps(run.id, first)) == len(demo.laps(run.id, first))


def strip_volatile(html: str) -> str:
    return re.sub(r'name="token" value="[^"]+"', "", html)


@pytest.mark.parametrize("page", ["today", "trends", "races", "segments", "data", "runs", "run"])
def test_pages_are_the_same(sources: Any, page: str) -> None:
    dbs, demo = sources
    renderer = Renderer(EN)
    run_id = next(a.id for a in dbs.runs() if a.has_records)
    build = {
        "today": ("overview.html", lambda s: pages.overview_page(s, EN, coach_name="Thresy")),
        "trends": ("trends.html", lambda s: pages.trends_page(s, EN)),
        "races": ("races.html", lambda s: pages.races_page(s, EN)),
        "segments": ("segments.html", lambda s: pages.segments_page(s, EN)),
        "data": ("data.html", lambda s: pages.data_page(s, EN)),
        "runs": ("runs.html", lambda s: pages.runs_page(s, EN, None)),
        "run": ("run.html", lambda s: pages.run_page(s, EN, run_id)),
    }
    template, fn = build[page]
    from_db = strip_volatile(renderer.render(template, fn(dbs)))
    from_demo = strip_volatile(renderer.render(template, fn(demo)))
    assert from_db == from_demo
