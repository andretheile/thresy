"""thresy.fit.parse_fit against the old parser's Parquet output for the same FIT files."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.legacy_parity import PICKS, pick
from thresy import store
from thresy.fit import FIT_INTENSITY, parse_fit

pytestmark = pytest.mark.legacy

# (our column, legacy column, absolute tolerance)
RECORD_PAIRS = [
    ("elapsed_s", "elapsed_s", 1e-9),
    ("dist_m", "dist_m", 1e-6),
    ("hr", "hr", 0),
    ("cadence_spm", "cadence", 1e-9),
    ("lat", "lat", 1e-9),
    ("lon", "lon", 1e-9),
    ("alt_m", "alt", 1e-6),
    ("speed_mps", "speed", 1e-9),
    ("power_w", "power", 0),
    ("resp_rate", "respiration_rate", 1e-9),
    ("temp_c", "temperature", 0),
]
LAP_PAIRS = [
    ("duration_s", "duration_s", 1e-9),
    ("elapsed_s", "elapsed_s", 1e-9),
    ("dist_m", "dist_m", 1e-6),
    ("avg_speed_mps", "avg_speed", 1e-9),
    ("max_speed_mps", "max_speed", 1e-9),
    ("avg_hr", "avg_hr", 0),
    ("max_hr", "max_hr", 0),
    ("avg_cadence_spm", "avg_cadence", 1e-9),
    ("avg_power_w", "avg_power", 0),
    ("ascent_m", "ascent_m", 0),
    ("descent_m", "descent_m", 0),
    ("avg_temp_c", "avg_temp", 0),
    ("wkt_step", "wkt_step", 0),
]


@pytest.fixture(scope="module")
def fit_ids(legacy_dir: Path) -> dict[str, tuple[Path, Path, Path | None]]:
    pytest.importorskip("pyarrow", reason="needs the legacy group: uv sync --group legacy")
    series = {p.stem: p for p in legacy_dir.glob("timeseries/year=*/*.parquet")}
    laps = {p.stem: p for p in legacy_dir.glob("laps/year=*/*.parquet")}
    ids = sorted(i for i in series if (legacy_dir / "tracks" / f"{i}.fit").is_file())
    if not ids:
        pytest.skip("no FIT file with a legacy Parquet counterpart")
    return {
        i: (legacy_dir / "tracks" / f"{i}.fit", series[i], laps.get(i))
        for i in (pick(ids, w) for w in PICKS)
    }


def _numeric(values: pd.Series) -> np.ndarray:
    return pd.to_numeric(values, errors="coerce").astype("float64").to_numpy()


def _legacy_intensity(value: object) -> str | None:
    """The old parser stored str(value); apply our normalisation to compare."""
    if value is None or (isinstance(value, float) and np.isnan(value)) or value == "None":
        return None
    text = str(value)
    return FIT_INTENSITY.get(int(text), text) if text.isdigit() else text


@pytest.mark.parametrize("which", PICKS)
def test_records_and_laps_match_legacy_parquet(fit_ids, which: str) -> None:
    ids = sorted(fit_ids)
    activity_id = pick(ids, which)
    fit_path, series_path, laps_path = fit_ids[activity_id]
    data = parse_fit(fit_path)

    legacy = pd.read_parquet(series_path)
    assert len(data.records) == len(legacy)
    ours = pd.DataFrame([asdict(r) for r in data.records])
    assert (pd.to_datetime(ours.ts, utc=True) == legacy.ts_utc).all()
    for col, legacy_col, tol in RECORD_PAIRS:
        np.testing.assert_allclose(
            _numeric(ours[col]), _numeric(legacy[legacy_col]), rtol=0, atol=tol,
            equal_nan=True, err_msg=f"record column {col}",
        )  # fmt: skip

    if laps_path is None:
        assert data.laps == []
        return
    legacy_laps = pd.read_parquet(laps_path)
    assert len(data.laps) == len(legacy_laps)
    our_laps = pd.DataFrame([asdict(lap) for lap in data.laps])
    assert list(our_laps.lap_no) == list(legacy_laps.lap)
    for col, legacy_col, tol in LAP_PAIRS:
        np.testing.assert_allclose(
            _numeric(our_laps[col]), _numeric(legacy_laps[legacy_col]), rtol=0, atol=tol,
            equal_nan=True, err_msg=f"lap column {col}",
        )  # fmt: skip
    assert list(our_laps.intensity) == [_legacy_intensity(v) for v in legacy_laps.intensity]
    assert list(our_laps.trigger) == [
        None if v in (None, "None") else v for v in legacy_laps.trigger
    ]
    assert (pd.to_datetime(our_laps.start_utc, utc=True) == legacy_laps.start_utc).all()


@pytest.mark.db
def test_records_load_into_postgres(fit_ids, legacy_dir: Path, conn) -> None:
    """Round trip of real records through COPY, for the first file that also has a summary."""
    candidates = sorted(
        p.stem
        for p in legacy_dir.glob("timeseries/year=*/*.parquet")
        if (legacy_dir / "tracks" / f"{p.stem}.fit").is_file()
        and (legacy_dir / "activities" / f"{p.stem}.json").is_file()
    )
    if not candidates:
        pytest.skip("no FIT file with both a Parquet counterpart and a summary JSON")
    activity_id = candidates[0]
    summary = legacy_dir / "activities" / f"{activity_id}.json"
    fit_path = legacy_dir / "tracks" / f"{activity_id}.fit"
    series_path = next(legacy_dir.glob(f"timeseries/year=*/{activity_id}.parquet"))
    data = parse_fit(fit_path)
    store.upsert_activity(conn, json.loads(summary.read_text()), source="legacy-test")
    n = store.replace_activity_records(conn, int(activity_id), data.records, data.laps)
    legacy = pd.read_parquet(series_path)
    row = conn.execute(
        "SELECT count(*) AS n, max(dist_m) AS dist, count(hr) AS hr_n, count(lat) AS lat_n"
        " FROM activity_records WHERE activity_id = %s",
        (int(activity_id),),
    ).fetchone()
    assert n == row["n"] == len(legacy)
    assert row["dist"] == pytest.approx(legacy.dist_m.max(), rel=1e-6)  # stored as real
    assert row["hr_n"] == legacy.hr.notna().sum()
    assert row["lat_n"] == legacy.lat.notna().sum()
    laps = conn.execute(
        "SELECT count(*) AS n FROM activity_laps WHERE activity_id = %s", (int(activity_id),)
    ).fetchone()["n"]
    assert laps == len(data.laps)
