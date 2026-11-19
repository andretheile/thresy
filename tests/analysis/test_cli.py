"""``coach analysis ...`` end to end on a synthetic database."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests import synthetic
from tests.analysis.synth import (
    M_PER_DEG,
    ORIGIN,
    T0,
    Phase,
    hill_out_and_back,
    interval_phases,
    laps_for,
    put_activity,
    records,
)
from thresy import store
from thresy.analysis.cli import app
from thresy.config import get_settings

runner = CliRunner()
D = date(2030, 3, 4)


@pytest.fixture
def env(monkeypatch, tmp_path, test_db):
    def use(url: str) -> None:
        monkeypatch.setenv("COACH_DATABASE_URL", url)
        get_settings.cache_clear()

    monkeypatch.setenv("COACH_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("COACH_LANGUAGE", "de")
    monkeypatch.setenv("COACH_TZ", "UTC")
    use(test_db.app_url)
    yield use
    get_settings.cache_clear()


@pytest.fixture
def data(conn):
    phases = interval_phases()
    triggers = ["manual"] * (len(phases) - 1) + ["session_end"]
    t1, t3 = T0 + timedelta(hours=3), T0 - timedelta(days=365)
    put_activity(conn, 601, records(phases, t0=t1), laps_for(phases, triggers, t0=t1), t0=t1)
    put_activity(conn, 602, records(hill_out_and_back(laps=2)), t0=T0)
    put_activity(conn, 603, records(hill_out_and_back(), t0=t3), t0=t3)
    store.upsert_wellness(
        conn, D, sleep=synthetic.sleep(D), hrv=synthetic.hrv(D), stats=synthetic.stats(D)
    )
    return conn


def run(*args: str) -> object:
    result = runner.invoke(app, list(args))
    return result


def as_json(*args: str) -> dict:
    result = run(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_help_is_a_map_of_the_commands() -> None:
    out = run("--help").output
    for name in (
        "list",
        "activity",
        "intervals",
        "intervals-compare",
        "sensor",
        "night",
        "wellness",
        "weeks",
        "segments",
        "route",
    ):
        assert name in out
    assert "--json" in out and "COACH_WORKSPACE_DIR/out" in out


@pytest.mark.db
def test_activity_selectors(env, data) -> None:
    listed = as_json("list", "--limit", "5")["activities"]
    assert [a["id"] for a in listed] == [601, 602, 603]
    assert as_json("activity")["activity"]["id"] == 601  # default: the last run
    by_date = as_json("activity", "--date", D.isoformat())
    assert by_date["activity"]["id"] == 601  # the longest run that day (8.9 km vs 6.8 km)
    assert by_date["selected"].startswith("longest run on")
    assert as_json("activity", D.isoformat())["activity"]["id"] == 601
    assert as_json("activity", "last")["activity"]["id"] == 601
    missing = run("activity", "999")
    assert missing.exit_code == 1 and "no activity with id 999" in missing.output
    both = run("activity", "601", "--last")
    assert both.exit_code == 1


@pytest.mark.db
def test_activity_text_and_plot(env, data, tmp_path) -> None:
    result = run("activity", "602", "--plot")
    assert result.exit_code == 0, result.output
    assert "splits (1000 m):" in result.output and "climbs (>= 25 m):" in result.output
    plot = tmp_path / "ws" / "out" / "activity-602.png"
    assert plot.is_file() and f"plot: {plot}" in result.output
    out = as_json("activity", "602")
    assert len(out["climbs"]) == 2 and out["totals"]["ascent_m"] == pytest.approx(160, abs=8)


@pytest.mark.db
def test_intervals(env, data, tmp_path) -> None:
    out = as_json("intervals", "601", "--plot")
    assert out["source"] == "laps" and len(out["reps"]) == 6
    assert Path(out["plot"]).is_file()
    assert out["reps"][0]["time_s"] == pytest.approx(180, abs=1)
    text = run("intervals", "601").output
    assert "6 reps from lap presses" in text and "drop60" in text
    records_only = as_json("intervals", "601", "--source", "records", "--signal", "power")
    assert records_only["source"] == "records" and len(records_only["reps"]) == 6
    steady = run("intervals", "602")
    assert steady.exit_code == 0


@pytest.mark.db
def test_intervals_compare(env, data) -> None:
    out = as_json("intervals-compare", "--rep-s", "180", "--plot")
    assert [s["activity_id"] for s in out["sessions"]] == [601]
    assert Path(out["plot"]).is_file()
    assert run("intervals-compare").exit_code == 2


@pytest.mark.db
def test_sensor(env, data) -> None:
    out = as_json("sensor", "601", "--plot")
    assert out["verdict"] == "ok" and Path(out["plot"]).is_file()


@pytest.mark.db
def test_segments_commands_with_the_agent_role(env, data, test_db, tmp_path) -> None:
    seg_file = tmp_path / "segments.toml"
    bottom = ORIGIN[0] + 501 / M_PER_DEG
    top = ORIGIN[0] + 1501 / M_PER_DEG
    seg_file.write_text(
        f'[[segments]]\nkey = "hill"\nname = "Hill"\nstart = [{bottom}, {ORIGIN[1]}]\n'
        f"end = [{top}, {ORIGIN[1]}]\ndist_min_m = 800\ndist_max_m = 1200\n"
    )
    env(test_db.agent_url)
    assert as_json("segments", "import", str(seg_file))["new"] == 1
    hist = as_json("segments", "history", "hill", "--plot")
    assert hist["count"] == 3 and hist["cache"]["cache"] == "written"
    assert Path(hist["plot"]).is_file()
    assert as_json("segments", "list")["segments"][0]["cached_efforts"] == 3
    assert as_json("segments", "history", "hill", "--since", D.isoformat())["count"] == 2
    match = as_json("segments", "match", "602")
    assert len(match["efforts"]) == 2
    env(test_db.ro_url)
    ro = as_json("segments", "history", "hill", "--refresh")
    assert ro["count"] == 3 and ro["cache"]["cache"] == "read-only"
    assert run("segments", "history", "nope").exit_code == 1


@pytest.mark.db
def test_route_commands(env, data) -> None:
    found = as_json("route", "find", "603")["matches"]
    assert found[0]["activity_id"] == 602 and found[0]["on_course"] > 0.95
    # the long straight interval run passes over the whole course but mostly runs elsewhere
    assert {m["activity_id"]: m["on_course"] for m in found}[601] < 0.5
    out = as_json("route", "compare", "603", "602", "--plot")
    assert abs(out["delta_s"]) < 15 and Path(out["plot"]).is_file()
    text = run("route", "compare", "603", "602").output
    assert "B - A" in text and "climbs on the course" in text


@pytest.mark.db
def test_night_wellness_weeks(env, data) -> None:
    night = as_json("night", "--date", D.isoformat(), "--plot")
    assert night["synced"] is True and night["hrv"]["n"] == 4 and Path(night["plot"]).is_file()
    not_yet = run("night", "--date", (D + timedelta(days=1)).isoformat())
    assert "not synced yet" in not_yet.output and D.isoformat() in not_yet.output
    well = as_json("wellness", "--end", D.isoformat(), "--days", "7", "--plot")
    assert well["summary"]["last7"]["nights"] == 1 and Path(well["plot"]).is_file()
    assert "wake date" in run("wellness", "--end", D.isoformat(), "--days", "3").output
    weeks = as_json("weeks", "--weeks", "2")
    assert len(weeks["weeks"]) == 2 and "ytd" in weeks


def test_labels_and_json_cleaning() -> None:
    import numpy as np

    from thresy.analysis import plots
    from thresy.analysis.cli import clean

    assert plots.labels("de")["hr"].startswith("Herzfrequenz")
    assert plots.labels("fr") == plots.labels("en") == plots.LABELS["en"]
    assert set(plots.LABELS["de"]) == set(plots.LABELS["en"])
    assert clean({"a": np.int64(3), "_x": 1, "b": [float("nan"), np.float64(1.5)]}) == {
        "a": 3,
        "b": [None, 1.5],
    }
