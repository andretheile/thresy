"""coach races: the race list, written with the agent's role."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from thresy.tools import races
from thresy.tools.dates import local_today

from .test_plan import run_on
from .test_send import coach

pytestmark = pytest.mark.db


@pytest.fixture
def env(test_db, tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    return {"COACH_DATABASE_URL": test_db.agent_url, "COACH_TZ": "UTC"}


def ok(args: list[str], env: dict[str, str]) -> Any:
    result = coach([*args, "--json"], env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_add_list_show(conn, env) -> None:
    race = ok(
        [
            "races", "add", "+20", "City Half", "--distance-km", "21.1", "--elev-m", "120",
            "--start", "09:30", "--location", "Springfield", "--priority", "a",
            "--goal", "sub 1:30", "--url", "https://example.org/half",
        ],
        env,
    )  # fmt: skip
    assert race["priority"] == "A" and race["status"] == "planned"
    assert race["start_time_local"] == "09:30:00"
    ok(["races", "add", "+40", "Trail 50k", "--status", "maybe"], env)
    long_ago = (local_today() - timedelta(days=100)).isoformat()
    recently = (local_today() - timedelta(days=10)).isoformat()
    ok(["races", "add", long_ago, "Old 10k", "--distance-km", "10"], env)
    ok(["races", "add", recently, "Parkrun test", "--priority", "C"], env)

    text = coach(["races", "list"], env).output
    assert "Upcoming" in text and "Past" in text
    assert "(in 20 days) A City Half · 21.1 km · +120 m · start 09:30 · Springfield" in text
    assert "[maybe]" in text and "Parkrun test" in text and "Old 10k" not in text
    assert [r["name"] for r in ok(["races", "list", "--upcoming"], env)] == [
        "City Half",
        "Trail 50k",
    ]
    assert [r["name"] for r in ok(["races", "list", "--past"], env)] == [
        "Old 10k",
        "Parkrun test",
    ]
    assert len(ok(["races", "list", "--all"], env)) == 4
    detail = coach(["races", "show", str(race["id"])], env).output
    assert "url: https://example.org/half" in detail


def test_duplicates_are_refused(conn, env) -> None:
    ok(["races", "add", "2031-05-01", "Spring 10k"], env)
    result = coach(["races", "add", "2031-05-01", "spring 10K"], env)
    assert result.exit_code == 1 and "use coach races change" in result.output


def test_change_and_notes(conn, env) -> None:
    race = ok(["races", "add", "+30", "Ultra", "--status", "maybe"], env)
    changed = ok(
        ["races", "change", str(race["id"]), "--status", "dns", "--add-note", "no childcare"],
        env,
    )
    assert changed["status"] == "dns"
    assert changed["notes"] == f"{local_today().isoformat()}: no childcare"
    again = ok(["races", "change", str(race["id"]), "--add-note", "maybe next year"], env)
    assert again["notes"].count("\n") == 1
    bad = coach(["races", "change", str(race["id"]), "--priority", "Z"], env)
    assert bad.exit_code == 1 and "use A, B or C" in bad.output
    bad = coach(["races", "change", str(race["id"]), "--start", "noon"], env)
    assert bad.exit_code == 1 and "not a start time" in bad.output


def test_result_official_time_and_activity(conn, env) -> None:
    day = local_today() - timedelta(days=2)
    run_on(conn, 901, day, km=5.0, hour=8)
    run_on(conn, 902, day, km=21.3, hour=10)
    race = ok(["races", "add", day.isoformat(), "City Half"], env)
    done = ok(
        ["races", "result", str(race["id"]), "--time", "1:24:05", "--place", "12th of 480"], env
    )
    row = done["race"]
    assert row["status"] == "done" and row["activity_id"] == 902
    assert row["result"]["time"] == "1:24:05" and row["result"]["time_s"] == 5045
    assert row["result"]["source"] == "official"
    assert row["result"]["watch"]["distance_km"] == 21.3
    assert row["result"]["place"] == "12th of 480"
    text = coach(["races", "list"], env).output
    assert "result: 1:24:05 (official), 12th of 480 → activity 902" in text


def test_result_from_the_watch_and_dnf(conn, env) -> None:
    day = local_today() - timedelta(days=1)
    run_on(conn, 911, day, km=10.0)
    race = ok(["races", "add", "yesterday", "Town 10k"], env)
    done = ok(["races", "result", str(race["id"])], env)
    assert done["race"]["result"]["source"] == "watch"
    assert done["race"]["result"]["time"] == "50:00"  # timer time, not moving time

    earlier = (local_today() - timedelta(days=3)).isoformat()
    other = ok(["races", "add", earlier, "Mountain race"], env)
    dnf = ok(["races", "result", str(other["id"]), "--status", "dnf", "--note", "calf"], env)
    assert dnf["race"]["status"] == "dnf" and dnf["race"]["activity_id"] is None
    assert "no run on" in dnf["remark"]


def test_delete_and_unknown_id(conn, env) -> None:
    race = ok(["races", "add", "+5", "Mistake"], env)
    ok(["races", "delete", str(race["id"])], env)
    result = coach(["races", "show", str(race["id"])], env)
    assert result.exit_code == 1 and f"no race #{race['id']}" in result.output


def test_reported_time(conn, env) -> None:
    race = ok(["races", "add", "2031-03-01", "Club 5k"], env)
    done = ok(["races", "result", str(race["id"]), "--time", "18:30", "--source", "reported"], env)
    assert done["race"]["result"]["source"] == "reported"
    bad = coach(["races", "result", str(race["id"]), "--time", "18:30", "--source", "rumour"], env)
    assert bad.exit_code == 1 and "use official or reported" in bad.output


def test_parse_duration() -> None:
    assert races.parse_duration("1:31:07") == 5467
    assert races.parse_duration("18:42.4") == pytest.approx(1122.4)
    with pytest.raises(races.RaceError):
        races.parse_duration("fast")
