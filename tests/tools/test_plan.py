"""coach plan: the agent's role writes planned_workouts; the week view shows the runs."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from tests import synthetic
from thresy import store
from thresy.tools.dates import local_today, week_start

from .test_send import coach

pytestmark = pytest.mark.db


@pytest.fixture
def env(test_db, tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)  # no .env file
    return {"COACH_DATABASE_URL": test_db.agent_url, "COACH_TZ": "UTC"}


def today() -> date:
    return local_today()


def run_on(conn, activity_id: int, day: date, *, km: float = 10.0, hour: int = 7) -> None:
    store.upsert_activity(
        conn,
        synthetic.activity(
            activity_id,
            start_local=f"{day.isoformat()} {hour:02d}:00:00",
            start_gmt=f"{day.isoformat()} {hour:02d}:00:00",
            distance=km * 1000,
            duration=km * 300,
            moving=km * 295,
        ),
        source="test",
    )


def ok(args: list[str], env: dict[str, str]) -> Any:
    result = coach([*args, "--json"], env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_add_and_show_the_week(conn, env) -> None:
    monday = week_start(today())
    run_on(conn, 501, monday, km=12.0)
    added = ok(
        [
            "plan", "add", monday.isoformat(), "Easy with strides", "--kind", "easy",
            "--distance-km", "12", "--hr", "<150", "-d", "4x100 m strides at the end",
        ],
        env,
    )  # fmt: skip
    assert added["target"] == {"distance_km": 12.0, "hr": "<150"}
    assert added["status"] == "planned" and added["kind"] == "easy"

    result = coach(["plan", "show"], env)
    assert result.exit_code == 0, result.output
    text = result.output
    assert f"#{added['id']} [" in text and "easy: Easy with strides · 12 km · HR <150" in text
    assert "4x100 m strides at the end" in text
    assert "ran 501 07:00 12.0 km" in text
    assert "Ran: 1 runs · 12 km" in text

    # coach plan without a sub-command shows the same week
    assert coach(["plan"], env).output == text

    view = ok(["plan", "show"], env)
    assert view["from"] == monday.isoformat()
    assert len(view["days"]) == 7
    assert view["totals"]["planned_km"] == 12.0 and view["totals"]["runs"] == 1


def test_done_links_the_only_run(conn, env) -> None:
    day = today()
    run_on(conn, 601, day)
    w = ok(["plan", "add", "today", "Threshold", "--kind", "threshold"], env)
    done = ok(["plan", "done", str(w["id"]), "--note", "felt easy"], env)
    assert done["workout"]["status"] == "done"
    assert done["workout"]["activity_id"] == 601
    assert done["workout"]["target"]["note"] == "felt easy"
    assert done["remark"] is None
    text = coach(["plan", "show"], env).output
    assert f"(linked to #{w['id']})" in text and "note: felt easy" in text


def test_done_with_several_runs_or_none(conn, env) -> None:
    day = today()
    run_on(conn, 701, day, hour=7)
    run_on(conn, 702, day, hour=17)
    w = ok(["plan", "add", "today", "Intervals"], env)
    done = ok(["plan", "done", str(w["id"])], env)
    assert done["workout"]["activity_id"] is None
    assert "several runs" in done["remark"] and "701, 702" in done["remark"]
    linked = ok(["plan", "done", str(w["id"]), "--activity", "702"], env)
    assert linked["workout"]["activity_id"] == 702

    later = ok(["plan", "add", "+3", "Long run", "--kind", "long"], env)
    nothing = ok(["plan", "done", str(later["id"])], env)
    assert "no unlinked run" in nothing["remark"]


def test_skip_and_link(conn, env) -> None:
    yesterday = today() - timedelta(days=1)
    run_on(conn, 801, yesterday)
    a = ok(["plan", "add", "yesterday", "Hills"], env)
    b = ok(["plan", "add", "yesterday", "Easy"], env)
    skipped = ok(["plan", "skip", str(a["id"]), "--note", "ill"], env)
    assert skipped["status"] == "skipped" and skipped["target"] == {"note": "ill"}
    linked = ok(["plan", "link", str(b["id"]), "801"], env)
    assert linked["status"] == "done" and linked["activity_id"] == 801
    missing = coach(["plan", "link", str(b["id"]), "999"], env)
    assert missing.exit_code == 1 and "no activity 999" in missing.output


def test_move_future_and_past(conn, env) -> None:
    future = ok(["plan", "add", "+2", "Long run", "--distance-km", "25"], env)
    moved = ok(["plan", "move", str(future["id"]), "+3"], env)
    assert moved["old"]["id"] == moved["new"]["id"] == future["id"]
    assert moved["new"]["date"] == (today() + timedelta(days=3)).isoformat()
    assert moved["new"]["status"] == "planned"

    past = ok(["plan", "add", "today", "Threshold", "--pace", "4:10-4:15"], env)
    moved = ok(["plan", "move", str(past["id"]), "tomorrow"], env)
    old, new = moved["old"], moved["new"]
    assert old["status"] == "moved" and old["target"]["moved_to"] == new["id"]
    assert new["date"] == (today() + timedelta(days=1)).isoformat()
    assert new["target"] == {"pace": "4:10-4:15", "moved_from": past["id"]}
    again = coach(["plan", "move", str(past["id"]), "+4"], env)
    assert again.exit_code == 1 and "only planned workouts can be moved" in again.output

    text = coach(["plan", "show", "--weeks", "2"], env).output
    assert f"→ moved to #{new['id']}" in text
    assert f"(moved here from #{past['id']})" in text


def test_change_merges_and_clears(conn, env) -> None:
    w = ok(["plan", "add", "fri", "Tempo", "--distance-km", "10", "--pace", "4:00"], env)
    changed = ok(["plan", "change", str(w["id"]), "--pace", "4:20", "--title", "Tempo run"], env)
    assert changed["target"] == {"distance_km": 10.0, "pace": "4:20"}
    assert changed["title"] == "Tempo run"
    cleared = ok(["plan", "change", str(w["id"]), "--clear-target", "--hr", "<160"], env)
    assert cleared["target"] == {"hr": "<160"}
    extra = ok(["plan", "change", str(w["id"]), "--target-json", '{"reps": "3x10 min"}'], env)
    assert extra["target"] == {"hr": "<160", "reps": "3x10 min"}
    nothing = coach(["plan", "change", str(w["id"])], env)
    assert nothing.exit_code == 1 and "nothing to change" in nothing.output


def test_past_unmarked_workouts_are_flagged(conn, env) -> None:
    ok(["plan", "add", "yesterday", "Easy"], env)
    view = ok(["plan", "show", "yesterday"], env)
    assert view["totals"]["unmarked_past"] == 1
    assert "[planned, not marked]" in coach(["plan", "show", "yesterday"], env).output


def test_delete(conn, env) -> None:
    w = ok(["plan", "add", "today", "Oops"], env)
    ok(["plan", "delete", str(w["id"])], env)
    result = coach(["plan", "delete", str(w["id"])], env)
    assert result.exit_code == 1 and f"no planned workout #{w['id']}" in result.output


def test_validation(conn, env) -> None:
    assert (
        "unknown kind 'treshold'"
        in coach(["plan", "add", "today", "x", "--kind", "treshold"], env).output
    )
    assert "not a date" in coach(["plan", "add", "someday", "x"], env).output
    assert (
        "not valid JSON" in coach(["plan", "add", "today", "x", "--target-json", "{"], env).output
    )
    w = ok(["plan", "add", "today", "x"], env)
    assert (
        "unknown status" in coach(["plan", "change", str(w["id"]), "--status", "maybe"], env).output
    )


def test_read_only_role_is_refused(conn, test_db, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {"COACH_DATABASE_URL": test_db.ro_url, "COACH_TZ": "UTC"}
    result = coach(["plan", "add", "today", "x"], env)
    assert result.exit_code == 1
    assert "may not change the plan" in result.output


def test_target_text() -> None:
    from thresy.tools.plan import target_text

    assert target_text({"pace": "4:10-4:15"}) == "pace 4:10-4:15/km"
    assert target_text({"pace": "4:10 on the flat"}) == "pace 4:10 on the flat"
    assert target_text({"distance_km": 12, "hr": "<150", "reps": "5x1 km"}) == (
        "12 km · HR <150 · reps: 5x1 km"
    )
