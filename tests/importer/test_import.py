"""The legacy importer on a tiny synthetic export built in a temporary directory."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests import synthetic
from tests.test_fit import _synthetic_fit
from thresy import store
from thresy.config import get_settings
from thresy.importer import legacy
from thresy.importer.cli import app

D1, D2, D3 = date(2030, 1, 1), date(2030, 1, 2), date(2030, 1, 3)
SYNC1 = "2030-01-02T06:30:00+00:00"
SYNC2 = "2030-01-03T06:45:00+00:00"


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _report(day: date, synced_at: str, **wellness: Any) -> dict[str, Any]:
    yesterday = date.fromordinal(day.toordinal() - 1)
    return {
        "date": day.isoformat(),
        "synced_at": synced_at,
        "wellness": {"yesterday": yesterday.isoformat(), "today": day.isoformat(), **wellness},
    }


def make_export(root: Path) -> Path:
    run = synthetic.activity(9001, start_local="2030-01-02 07:00:00",
                             start_gmt="2030-01-02 06:00:00", name="Overview name")  # fmt: skip
    ride = synthetic.activity(9002, type_key="cycling", start_local="2030-01-03 10:00:00",
                              start_gmt="2030-01-03 09:00:00", name="Ride")  # fmt: skip
    later = synthetic.activity(9003, start_local="2030-01-05 18:00:00",
                               start_gmt="2030-01-05 17:00:00", name="Evening run")  # fmt: skip
    _write(root / "overviews" / "year-2030.json", [run, ride])
    _write(root / "overviews" / "ytd-2030.json", [{**run, "activityName": "Later overview"}])
    _write(root / "activities" / "9001.json", {**run, "activityName": "Final name"})
    _write(root / "activities" / "9003.json", later)
    (root / "tracks").mkdir()
    (root / "tracks" / "9001.fit").write_bytes(_synthetic_fit())
    (root / "tracks" / "9001.csv").write_text("ignored")
    (root / "tracks" / "9003.gpx").write_text("ignored")
    _write(
        root / "daily" / "2030-01-02.json",
        _report(
            D2, SYNC1,
            sleep_yesterday=synthetic.sleep(D1, score=70),
            hrv_yesterday=synthetic.hrv(D1, night_avg=55),
            stats_yesterday=synthetic.stats(D1, steps=12000),
            sleep_today=synthetic.sleep(D2, synced=False),  # not uploaded yet
            hrv_today=[],
            stats_today=synthetic.stats(D2, steps=300),
            steps_today=[{"steps": 300}],
            sleep_daily_yesterday={"error": "HTTPError: 404"},
        ),
    )  # fmt: skip
    _write(
        root / "daily" / "2030-01-03.json",
        _report(
            D3, SYNC2,
            sleep_yesterday=synthetic.sleep(D2, score=81),
            hrv_yesterday=synthetic.hrv(D2, night_avg=62),
            stats_yesterday=synthetic.stats(D2, steps=9000),
            sleep_today=synthetic.sleep(D3, score=77),
            hrv_today=synthetic.hrv(D3, night_avg=58),
            stats_today=synthetic.stats(D3, steps=None),
        ),
    )  # fmt: skip
    _write(root / "daily" / "latest.json", {"ignored": True})
    _write(
        root / "overviews" / "personal-records.json",
        [
            {"typeId": 3, "value": 1500.5, "activityId": 9001, "activityName": "Final name",
             "activityStartDateTimeLocalFormatted": "2030-01-02T07:00:00.0"},
            {"typeId": 12, "value": 30000.0, "activityId": 0},
        ],
    )  # fmt: skip
    _write(
        root / "overviews" / "pr-hm-marathon.json",
        {"source": "x", "half_marathon_pr": {"id": 9001, "name": "Final name",
                                             "date": "2030-01-02", "time": "1:30:05"},
         "note": "text"},
    )  # fmt: skip
    return root


@pytest.fixture
def export(tmp_path) -> Path:
    return make_export(tmp_path / "legacy")


def _snapshot(conn) -> dict[str, list[dict]]:
    tables = ("activities", "activity_records", "activity_laps", "wellness_daily",
              "sleep_levels", "hrv_readings", "personal_records", "events")  # fmt: skip
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}


def test_report_payloads_key_by_own_calendar_date() -> None:
    report = _report(
        D3, SYNC2,
        sleep_yesterday=synthetic.sleep(D2),
        sleep_today=synthetic.sleep(D2, synced=False),  # a placeholder for the same night
        hrv_today=synthetic.hrv(D2),  # labelled "today" but belongs to D2
        stats_today={"error": "boom"},
        steps_today=[{"steps": 5}],
    )  # fmt: skip
    grouped = legacy.report_payloads(report)
    assert set(grouped) == {D2, D3}
    assert store.is_real_sleep(grouped[D2]["sleep"])  # real sleep beats the placeholder
    assert "hrv" in grouped[D2] and grouped[D3] == {"steps": [{"steps": 5}]}


@pytest.mark.db
def test_full_import(conn, export, tmp_path) -> None:
    data_dir = tmp_path / "data"
    summary = legacy.run_import(conn, export, data_dir)
    assert summary.errors == []
    assert summary.activities["new"] == 3
    assert summary.records["imported"] == 1 and summary.records["records"] == 5
    assert summary.prs["new"] == 3

    acts = {r["id"]: r for r in conn.execute("SELECT * FROM activities").fetchall()}
    assert acts[9001]["name"] == "Final name"  # activities/*.json win over the overviews
    assert acts[9002]["is_run"] is False and acts[9003]["has_records"] is False
    assert {r["source"] for r in acts.values()} == {"legacy"}
    assert acts[9001]["fit_path"] == "fit/2030/9001.fit"
    assert (data_dir / "fit" / "2030" / "9001.fit").read_bytes() == (
        export / "tracks" / "9001.fit"
    ).read_bytes()
    assert acts[9001]["has_records"] is True
    assert conn.execute("SELECT count(*) AS n FROM activity_laps").fetchone()["n"] == 1

    well = {r["wake_date"]: r for r in conn.execute("SELECT * FROM wellness_daily").fetchall()}
    assert set(well) == {D1, D2, D3}
    assert well[D1]["sleep_synced_at"] == datetime(2030, 1, 2, 6, 30, tzinfo=UTC)
    # the placeholder in the first report does not count: D2's night arrived with the second
    assert well[D2]["sleep_synced_at"] == datetime(2030, 1, 3, 6, 45, tzinfo=UTC)
    assert well[D2]["sleep_score"] == 81 and well[D2]["hrv_night_avg"] == 62
    assert well[D2]["steps"] == 9000 and well[D1]["steps"] == 12000
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0

    prs = {r["key"]: r for r in conn.execute("SELECT * FROM personal_records").fetchall()}
    assert prs["garmin_3"]["value"] == 1500.5 and prs["garmin_3"]["date"] == D2
    assert prs["garmin_12"]["activity_id"] is None
    assert (
        prs["computed_half_marathon_pr"]["value"] == 5405
        and prs["computed_half_marathon_pr"]["source"] == "computed"
    )


@pytest.mark.db
def test_second_run_changes_nothing(conn, export, tmp_path) -> None:
    data_dir = tmp_path / "data"
    legacy.run_import(conn, export, data_dir)
    before = _snapshot(conn)
    fit = data_dir / "fit" / "2030" / "9001.fit"
    mtime = fit.stat().st_mtime_ns
    again = legacy.run_import(conn, export, data_dir)
    assert _snapshot(conn) == before  # updated_at included
    assert fit.stat().st_mtime_ns == mtime
    assert again.activities["unchanged"] == 3 and "new" not in again.activities
    assert again.records["unchanged"] == 1 and "imported" not in again.records
    assert again.wellness["reports_unchanged"] == 2 and "upserts" not in again.wellness
    assert again.prs["new"] == 0


@pytest.mark.db
def test_resume_and_changed_reports(conn, export, tmp_path) -> None:
    data_dir = tmp_path / "data"
    legacy.run_import(conn, export, data_dir, parts=["activities"])
    assert conn.execute("SELECT count(*) AS n FROM activity_records").fetchone()["n"] == 0
    resumed = legacy.run_import(conn, export, data_dir)
    assert resumed.activities["unchanged"] == 3 and resumed.records["imported"] == 1
    # a changed report is applied again, with every report after it
    path = export / "daily" / "2030-01-02.json"
    report = json.loads(path.read_text())
    report["wellness"]["stats_yesterday"]["restingHeartRate"] = 44
    path.write_text(json.dumps(report))
    changed = legacy.run_import(conn, export, data_dir, parts=["wellness"])
    assert changed.wellness["reports_unchanged"] == 0 and changed.wellness["reports_imported"] == 2
    row = conn.execute("SELECT resting_hr FROM wellness_daily WHERE wake_date = %s", (D1,))
    assert row.fetchone()["resting_hr"] == 44


@pytest.mark.db
def test_selection_limits_activities(conn, export, tmp_path) -> None:
    newest = legacy.run_import(conn, export, tmp_path / "data", selection=legacy.Selection(limit=1),
                               parts=["activities", "records"])  # fmt: skip
    assert newest.activities["new"] == 1 and newest.records["files"] == 0
    assert [r["id"] for r in conn.execute("SELECT id FROM activities")] == [9003]
    chosen = legacy.Selection(ids=frozenset({9001, 9002}), until=D2)
    legacy.run_import(conn, export, tmp_path / "data", selection=chosen)
    assert {r["id"] for r in conn.execute("SELECT id FROM activities")} == {9001, 9003}


@pytest.mark.db
def test_data_from_another_source_is_kept(conn, export, tmp_path) -> None:
    store.upsert_activity(conn, synthetic.activity(9001, name="From Garmin"), source="garmin")
    store.upsert_wellness(conn, D3, stats=synthetic.stats(D3, resting_hr=39))
    summary = legacy.run_import(conn, export, tmp_path / "data")
    assert summary.activities["skipped_other_source"] == 1
    assert summary.wellness["skipped_other_source"] == 1
    assert conn.execute("SELECT name FROM activities WHERE id = 9001").fetchone()["name"] == (
        "From Garmin"
    )
    row = conn.execute("SELECT resting_hr FROM wellness_daily WHERE wake_date = %s", (D3,))
    assert row.fetchone()["resting_hr"] == 39
    # a different FIT already stored for the activity is not replaced
    other = tmp_path / "data2" / "fit" / "2030" / "9001.fit"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"another original")
    again = legacy.run_import(conn, export, tmp_path / "data2", parts=["records"])
    assert again.records["skipped_other_file"] == 1
    assert other.read_bytes() == b"another original"


@pytest.mark.db
def test_broken_fit_is_reported(conn, export, tmp_path) -> None:
    (export / "tracks" / "9003.fit").write_bytes(b"not a fit file")
    summary = legacy.run_import(conn, export, tmp_path / "data")
    assert summary.records["failed"] == 1 and summary.records["imported"] == 1
    assert "9003.fit" in summary.errors[0]


def test_missing_directory_and_unknown_part(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        legacy.LegacyExport(tmp_path / "nope").check()
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="not a legacy export"):
        legacy.LegacyExport(tmp_path / "empty").check()
    with pytest.raises(ValueError, match="unknown part"):
        legacy.run_import(None, tmp_path, tmp_path, parts=["everything"])  # type: ignore[arg-type]


@pytest.mark.db
def test_cli(conn, export, tmp_path, test_db, monkeypatch) -> None:
    monkeypatch.setenv("COACH_DATABASE_URL", test_db.app_url)
    monkeypatch.delenv("COACH_LEGACY_DIR", raising=False)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        args = ["legacy", "--dir", str(export), "--data-dir", str(tmp_path / "data")]
        result = runner.invoke(app, [*args, "--json", "--only", "activities", "--only", "prs"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output[result.output.index("{") :])
        assert out["activities"]["new"] == 3 and out["records"] == {} and out["prs"]["new"] == 3
        text = runner.invoke(app, [*args, "--quiet", "--since", "2030-01-05"])
        assert text.exit_code == 0 and "activities  unchanged 1" in text.output
        assert runner.invoke(app, ["legacy"]).exit_code == 1  # no directory configured
        bad = runner.invoke(app, [*args, "--since", "yesterday"])
        assert bad.exit_code == 2
    finally:
        get_settings.cache_clear()
