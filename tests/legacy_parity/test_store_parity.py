"""thresy.store upserts against the old bot's runs.db for the same raw payloads."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.legacy_parity import PICKS, pick
from thresy import store

pytestmark = [pytest.mark.legacy, pytest.mark.db]

# (our column, legacy column, converter for ours, absolute tolerance)
ACTIVITY_PAIRS: list[tuple[str, str, Any, float]] = [
    ("distance_m", "distance_km", lambda v: v / 1000, 6e-4),  # legacy rounds km to 3 places
    ("duration_s", "duration_s", None, 1e-6),
    ("moving_s", "moving_s", None, 1e-6),
    ("avg_hr", "avg_hr", None, 1e-3),
    ("max_hr", "max_hr", None, 1e-3),
    ("elev_gain_m", "elev_m", None, 1e-3),
    ("elev_loss_m", "elev_loss_m", None, 1e-3),
    ("avg_cadence_spm", "cadence", None, 1e-3),
    ("avg_power_w", "avg_power", None, 1e-3),
    ("vo2max", "vo2max", None, 1e-3),
    ("aerobic_te", "aerobic_te", None, 1e-4),
    ("anaerobic_te", "anaerobic_te", None, 1e-4),
    ("training_load", "training_load", None, 1e-3),
    ("calories", "calories", None, 1e-3),
]
WELLNESS_PAIRS = [
    ("sleep_score", "sleep_score"),
    ("sleep_s", "sleep_s"),
    ("deep_s", "deep_s"),
    ("light_s", "light_s"),
    ("rem_s", "rem_s"),
    ("awake_s", "awake_s"),
    ("hrv_night_avg", "hrv_avg"),
    ("hrv_weekly_avg", "hrv_weekly_avg"),
    ("hrv_status", "hrv_status"),
    ("steps", "steps"),
    ("resting_hr", "resting_hr"),
]


@pytest.fixture(scope="module")
def runs_db(legacy_dir: Path) -> Iterator[sqlite3.Connection]:
    path = legacy_dir / "runs.db"
    if not path.is_file():
        pytest.skip("legacy runs.db not found")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    yield con
    con.close()


def _close(ours: Any, theirs: Any, tol: float) -> bool:
    if ours is None or theirs is None:
        return ours is None and theirs is None
    return abs(float(ours) - float(theirs)) <= tol * max(1.0, abs(float(theirs)))


@pytest.mark.parametrize("which", PICKS)
def test_activity_matches_runs_db(legacy_dir: Path, runs_db, conn, which: str) -> None:
    legacy_ids = {r["id"] for r in runs_db.execute("SELECT id FROM activities")}
    files = sorted(p for p in (legacy_dir / "activities").glob("*.json") if p.stem in legacy_ids)
    if not files:
        pytest.skip("no activity JSON with a runs.db row")
    payload = json.loads(pick(files, which).read_text())
    assert store.upsert_activity(conn, payload, source="legacy-test") is True
    activity_id = int(payload["activityId"])
    ours = conn.execute("SELECT * FROM activities WHERE id = %s", (activity_id,)).fetchone()
    theirs = dict(
        runs_db.execute("SELECT * FROM activities WHERE id = ?", (str(activity_id),)).fetchone()
    )

    assert ours["local_date"].isoformat() == theirs["date"]
    assert ours["start_local"].isoformat(sep=" ") == theirs["start_local"]
    assert ours["type"] == theirs["type"]
    assert ours["name"] == theirs["name"]
    assert ours["location"] == theirs["location"]
    assert int(ours["is_run"]) == theirs["is_run"]
    for col, legacy_col, convert, tol in ACTIVITY_PAIRS:
        value = ours[col]
        if convert and value is not None:
            value = convert(value)
        assert _close(value, theirs[legacy_col], tol), col

    if ours["is_run"]:
        run = conn.execute("SELECT * FROM runs WHERE id = %s", (activity_id,)).fetchone()
        ours_pace, theirs_pace = run["pace_s_per_km"], theirs["pace_s_per_km"]
        if theirs_pace is None or ours_pace is None:
            assert ours_pace == theirs_pace
        else:
            # The old store rounded pace to 0.1 s and divided by the distance rounded to 0.001 km;
            # the view uses the exact distance.
            tol = 0.051 + ours_pace * 0.0005 / theirs["distance_km"]
            assert abs(ours_pace - theirs_pace) <= tol
            if int(ours_pace) == int(theirs_pace):
                assert run["pace"] == theirs["pace"]


def _reports_for(legacy_dir: Path, day: str) -> list[tuple[str, dict[str, Any]]]:
    """(tag, wellness block) of every daily report that covers ``day``, in file order."""
    out = []
    for path in sorted((legacy_dir / "daily").glob("20*.json")):
        wellness = json.loads(path.read_text()).get("wellness") or {}
        for tag in ("yesterday", "today"):
            if wellness.get(tag) == day:
                out.append((tag, wellness))
    return out


@pytest.mark.parametrize("which", PICKS)
def test_wellness_matches_runs_db(legacy_dir: Path, runs_db, conn, which: str) -> None:
    days = sorted(r["date"] for r in runs_db.execute("SELECT date FROM wellness_daily"))
    if not days:
        pytest.skip("legacy wellness_daily is empty")
    day = pick(days, which)
    reports = _reports_for(legacy_dir, day)
    if not reports:
        pytest.skip("no daily report covers the chosen day")
    for tag, w in reports:
        store.upsert_wellness(
            conn,
            date.fromisoformat(day),
            sleep=w.get(f"sleep_{tag}"),
            hrv=w.get(f"hrv_{tag}"),
            stats=w.get(f"stats_{tag}"),
            steps=w.get(f"steps_{tag}"),
        )
    ours = conn.execute("SELECT * FROM wellness_daily WHERE wake_date = %s", (day,)).fetchone()
    theirs = dict(runs_db.execute("SELECT * FROM wellness_daily WHERE date = ?", (day,)).fetchone())
    for col, legacy_col in WELLNESS_PAIRS:
        assert ours[col] == theirs[legacy_col], col

    # The stage mapping holds on real data: per-stage durations add up to the DTO totals.
    if ours["sleep_s"]:
        totals: dict[str, float] = defaultdict(float)
        for r in conn.execute(
            "SELECT stage, extract(epoch FROM end_utc - start_utc) AS s FROM sleep_levels"
            " WHERE wake_date = %s",
            (day,),
        ):
            totals[r["stage"]] += float(r["s"])
        if totals:
            for stage, col in (("deep", "deep_s"), ("light", "light_s"), ("rem", "rem_s"),
                               ("awake", "awake_s")):  # fmt: skip
                assert abs(totals[stage] - (ours[col] or 0)) <= 60, stage
