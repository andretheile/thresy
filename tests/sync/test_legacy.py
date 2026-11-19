"""Real payloads from the old bot's export pushed through the sync pass (fake client).

Skipped unless ``COACH_LEGACY_DIR`` is set. Inputs are chosen dynamically (first, middle and
last of what the export contains) and the results are compared with the old bot's own store
(``runs.db``, the FIT files in ``tracks/`` and, with the ``legacy`` dependency group, the
Parquet record files), never with numbers written here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.legacy_parity import PICKS, pick
from tests.legacy_parity.test_store_parity import ACTIVITY_PAIRS, WELLNESS_PAIRS
from tests.sync.helpers import make_config, zipped
from thresy import store
from thresy.fit import parse_fit
from thresy.garmin.fake import FakeGarminClient
from thresy.sync.passes import run_pass

pytestmark = [pytest.mark.legacy, pytest.mark.db]

PARTS = ("sleep", "hrv", "stats", "steps")


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


def _reports_for(legacy_dir: Path, day: str) -> list[tuple[str, dict[str, Any]]]:
    """(tag, wellness block) of every daily report that covers ``day``, in file order."""
    out = []
    for path in sorted((legacy_dir / "daily").glob("20*.json")):
        wellness = json.loads(path.read_text()).get("wellness") or {}
        for tag in ("yesterday", "today"):
            if wellness.get(tag) == day:
                out.append((tag, wellness))
    return out


def _fake_for_report(wellness: dict[str, Any]) -> FakeGarminClient:
    """A Garmin that answers what the old bot received when it wrote this report."""
    fake = FakeGarminClient()
    for tag in ("yesterday", "today"):
        if not wellness.get(tag):
            continue
        day = date.fromisoformat(wellness[tag])
        fake.set_wellness(day, **{part: wellness.get(f"{part}_{tag}") for part in PARTS})
    return fake


@pytest.mark.parametrize("which", PICKS)
def test_wellness_through_the_pass_matches_runs_db(
    legacy_dir: Path, runs_db, conn, tmp_path, which: str
) -> None:
    days = sorted(r["date"] for r in runs_db.execute("SELECT date FROM wellness_daily"))
    if not days:
        pytest.skip("legacy wellness_daily is empty")
    day = pick(days, which)
    reports = _reports_for(legacy_dir, day)
    if not reports:
        pytest.skip("no daily report covers the chosen day")
    config = make_config(tmp_path)
    for _tag, wellness in reports:
        today = date.fromisoformat(wellness["today"])
        # A pass at noon (UTC zone here) of the report's "today": its today and yesterday are
        # the pass's today and yesterday.
        run_pass(conn, _fake_for_report(wellness), config,
                 now=datetime.combine(today, time(12), tzinfo=UTC))  # fmt: skip

    ours = conn.execute("SELECT * FROM wellness_daily WHERE wake_date = %s", (day,)).fetchone()
    theirs = dict(runs_db.execute("SELECT * FROM wellness_daily WHERE date = ?", (day,)).fetchone())
    for col, legacy_col in WELLNESS_PAIRS:
        assert ours[col] == theirs[legacy_col], col

    first_real = next(
        (tag for tag, w in reports if store.is_real_sleep(w.get(f"sleep_{tag}"))), None
    )
    arrived = conn.execute(
        "SELECT count(*) AS n FROM events WHERE kind = 'sleep_arrived' AND dedupe_key = %s",
        (f"sleep:{day}",),
    ).fetchone()["n"]
    # One event exactly when the night first showed up as "today's" sleep, never otherwise.
    assert arrived == (1 if first_real == "today" else 0)
    if first_real is not None:
        assert ours["sleep_synced_at"] is not None


def _activity_candidates(legacy_dir: Path, runs_db) -> list[str]:
    legacy_ids = {r["id"] for r in runs_db.execute("SELECT id FROM activities")}
    return sorted(
        p.stem
        for p in (legacy_dir / "activities").glob("*.json")
        if p.stem in legacy_ids and (legacy_dir / "tracks" / f"{p.stem}.fit").is_file()
    )


@pytest.mark.parametrize("which", PICKS)
def test_new_activity_through_the_pass_matches_the_legacy_store(
    legacy_dir: Path, runs_db, conn, tmp_path, which: str
) -> None:
    candidates = _activity_candidates(legacy_dir, runs_db)
    if not candidates:
        pytest.skip("no activity with a summary, a runs.db row and a FIT file")
    activity_id = pick(candidates, which)
    summary = json.loads((legacy_dir / "activities" / f"{activity_id}.json").read_text())
    fit_bytes = (legacy_dir / "tracks" / f"{activity_id}.fit").read_bytes()
    fake = FakeGarminClient()
    fake.add_activity(summary, original=zipped(fit_bytes, f"{activity_id}_ACTIVITY.fit"))
    row = store.activity_row(summary)
    start = row["start_utc"] or datetime.combine(row["local_date"], time(12), tzinfo=UTC)
    config = make_config(tmp_path)

    result = run_pass(conn, fake, config, now=start + timedelta(hours=1))
    assert result.new_activities == [int(activity_id)] == result.fit_stored

    ours = conn.execute("SELECT * FROM activities WHERE id = %s", (int(activity_id),)).fetchone()
    theirs = dict(
        runs_db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    )
    assert ours["local_date"].isoformat() == theirs["date"]
    assert ours["type"] == theirs["type"] and int(ours["is_run"]) == theirs["is_run"]
    for col, legacy_col, convert, tol in ACTIVITY_PAIRS:
        value = ours[col]
        if convert and value is not None:
            value = convert(value)
        assert _close(value, theirs[legacy_col], tol), col

    # The original is stored where the contract says, byte for byte, and parsed into records.
    year = (ours["start_local"] or ours["local_date"]).year
    assert ours["fit_path"] == f"fit/{year:04d}/{activity_id}.fit"
    assert (config.data_dir / ours["fit_path"]).read_bytes() == fit_bytes
    parsed = parse_fit(fit_bytes)
    n = conn.execute(
        "SELECT count(*) AS n FROM activity_records WHERE activity_id = %s", (int(activity_id),)
    ).fetchone()["n"]
    assert n == len(parsed.records) and ours["has_records"] == bool(parsed.records)
    laps = conn.execute(
        "SELECT count(*) AS n FROM activity_laps WHERE activity_id = %s", (int(activity_id),)
    ).fetchone()["n"]
    assert laps == len(parsed.laps)
    series = list(legacy_dir.glob(f"timeseries/year=*/{activity_id}.parquet"))
    if series and _has_pyarrow():  # the old bot's own records (uv sync --group legacy)
        import pandas as pd

        assert n == len(pd.read_parquet(series[0]))

    events = conn.execute("SELECT kind, payload FROM events").fetchall()
    assert events == [
        {
            "kind": "new_activity",
            "payload": {
                "activity_id": int(activity_id),
                "type": theirs["type"],
                "is_run": bool(theirs["is_run"]),
            },
        }
    ]


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True
