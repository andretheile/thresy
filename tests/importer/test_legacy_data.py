"""The importer on the old bot's real export (``COACH_LEGACY_DIR``), compared with ``runs.db``.

Inputs are chosen dynamically (first, middle and last activity that has a FIT file), and every
expected value comes from the old store, never from numbers written here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from tests.legacy_parity import PICKS, pick
from thresy import store
from thresy.fit import parse_fit
from thresy.importer import legacy

pytestmark = [pytest.mark.legacy, pytest.mark.db]


@pytest.fixture(scope="module")
def runs_db(legacy_dir: Path) -> Iterator[sqlite3.Connection]:
    path = legacy_dir / "runs.db"
    if not path.is_file():
        pytest.skip("legacy runs.db not found")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    yield con
    con.close()


@pytest.fixture(scope="module")
def picked(legacy_dir: Path, runs_db) -> list[int]:
    in_db = {r["id"] for r in runs_db.execute("SELECT id FROM activities")}
    ids = sorted(int(p.stem) for p in (legacy_dir / "tracks").glob("*.fit") if p.stem in in_db)
    if not ids:
        pytest.skip("no FIT file with a runs.db row")
    return sorted({pick(ids, w) for w in PICKS})


def test_export_holds_exactly_the_activities_of_runs_db(legacy_dir: Path, runs_db) -> None:
    payloads = legacy.LegacyExport(legacy_dir).activity_payloads()
    assert {str(i) for i in payloads} == {
        r["id"] for r in runs_db.execute("SELECT id FROM activities")
    }


def test_activities_and_records(conn, legacy_dir: Path, runs_db, picked, tmp_path) -> None:
    data_dir = tmp_path / "data"
    selection = legacy.Selection(ids=frozenset(picked))
    summary = legacy.run_import(conn, legacy_dir, data_dir, parts=["activities", "records"],
                                selection=selection)  # fmt: skip
    assert summary.errors == []
    assert summary.activities["new"] == len(picked)
    assert summary.records["imported"] == len(picked)
    for activity_id in picked:
        ours = conn.execute("SELECT * FROM activities WHERE id = %s", (activity_id,)).fetchone()
        theirs = runs_db.execute(
            "SELECT * FROM activities WHERE id = ?", (str(activity_id),)
        ).fetchone()
        assert ours["local_date"].isoformat() == theirs["date"]
        assert (ours["type"], ours["name"], int(ours["is_run"])) == (
            theirs["type"], theirs["name"], theirs["is_run"],
        )  # fmt: skip
        assert round(ours["distance_m"] / 1000, 3) == pytest.approx(theirs["distance_km"])
        assert ours["duration_s"] == pytest.approx(theirs["duration_s"])
        assert ours["source"] == "legacy"
        src = legacy_dir / "tracks" / f"{activity_id}.fit"
        fit = parse_fit(src)
        n = conn.execute(
            "SELECT count(*) AS n FROM activity_records WHERE activity_id = %s", (activity_id,)
        ).fetchone()["n"]
        laps = conn.execute(
            "SELECT count(*) AS n FROM activity_laps WHERE activity_id = %s", (activity_id,)
        ).fetchone()["n"]
        assert (n, laps) == (len(fit.records), len(fit.laps))
        assert ours["fit_path"] == f"fit/{ours['start_local'].year}/{activity_id}.fit"
        assert (data_dir / ours["fit_path"]).read_bytes() == src.read_bytes()
    again = legacy.run_import(conn, legacy_dir, data_dir, parts=["activities", "records"],
                              selection=selection)  # fmt: skip
    assert again.activities["unchanged"] == len(picked)
    assert again.records["unchanged"] == len(picked)


WELLNESS_PAIRS = [
    ("sleep_score", "sleep_score"), ("sleep_s", "sleep_s"), ("deep_s", "deep_s"),
    ("light_s", "light_s"), ("rem_s", "rem_s"), ("awake_s", "awake_s"),
    ("hrv_night_avg", "hrv_avg"), ("hrv_weekly_avg", "hrv_weekly_avg"),
    ("hrv_status", "hrv_status"), ("steps", "steps"), ("resting_hr", "resting_hr"),
]  # fmt: skip


def test_wellness_matches_runs_db(conn, legacy_dir: Path, runs_db, tmp_path) -> None:
    summary = legacy.run_import(conn, legacy_dir, tmp_path / "data", parts=["wellness"])
    assert summary.errors == []
    ours = {r["wake_date"].isoformat(): r for r in conn.execute("SELECT * FROM wellness_daily")}
    theirs = {r["date"]: dict(r) for r in runs_db.execute("SELECT * FROM wellness_daily")}
    assert set(theirs) <= set(ours)
    for day, row in theirs.items():
        for col, legacy_col in WELLNESS_PAIRS:
            assert ours[day][col] == row[legacy_col], (day, col)

    # each night's sleep_synced_at is the sync time of the first report with real sleep for it
    first_seen: dict[str, str] = {}
    for path in sorted((legacy_dir / "daily").glob("20*.json")):
        report = json.loads(path.read_text())
        for day, payloads in legacy.report_payloads(report).items():
            if store.is_real_sleep(payloads.get("sleep")):
                first_seen.setdefault(day.isoformat(), report["synced_at"])
    for day, row in ours.items():
        expected = first_seen.get(day)
        if expected is None:
            assert row["sleep_synced_at"] is None, day
        else:
            assert row["sleep_synced_at"] == store.parse_garmin_time(expected, utc=True), day
    # every night with sleep has its stages, and stage totals match the summary
    for day, row in ours.items():
        if row["sleep_s"]:
            totals = conn.execute(
                "SELECT stage, sum(extract(epoch FROM end_utc - start_utc)) AS s"
                " FROM sleep_levels WHERE wake_date = %s GROUP BY stage",
                (date.fromisoformat(day),),
            ).fetchall()
            got = {r["stage"]: float(r["s"]) for r in totals}
            assert abs(got.get("rem", 0) - (row["rem_s"] or 0)) <= 60, day
            assert abs(got.get("awake", 0) - (row["awake_s"] or 0)) <= 60, day


def test_personal_records_match_runs_db(conn, legacy_dir: Path, runs_db, tmp_path) -> None:
    legacy.run_import(conn, legacy_dir, tmp_path / "data", parts=["prs"])
    ours = {r["key"]: r for r in conn.execute("SELECT * FROM personal_records")}
    theirs = {r["key"]: dict(r) for r in runs_db.execute("SELECT * FROM prs")}
    assert set(ours) == set(theirs)
    for key, row in theirs.items():
        assert ours[key]["value"] == pytest.approx(row["value"]), key
        legacy_id = int(row["activity_id"]) if row["activity_id"] not in (None, "", "0") else None
        assert ours[key]["activity_id"] == legacy_id, key
        assert (ours[key]["date"].isoformat() if ours[key]["date"] else "") == (row["date"] or "")
