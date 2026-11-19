"""The segment matcher against the old bot's ``match_segment`` on real data.

Both the legacy function and its segment definitions are loaded at test time from
``COACH_LEGACY_DIR/ts_store.py``; the activities are chosen dynamically among those where the
legacy matcher finds efforts. Our matcher runs on the records the importer stored in Postgres,
the legacy one on its own Parquet files.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from tests.legacy_parity import PICKS, pick
from thresy.analysis import segments as sg
from thresy.analysis.data import load_records
from thresy.importer import legacy

pytestmark = [pytest.mark.legacy, pytest.mark.db]

COLUMNS = ["activity_id", "ts_local", "elapsed_s", "lat", "lon", "alt", "dist_m", "hr"]


@pytest.fixture(scope="module")
def ts_store(legacy_dir: Path) -> ModuleType:
    pytest.importorskip("pyarrow", reason="needs the legacy group: uv sync --group legacy")
    path = legacy_dir / "ts_store.py"
    if not path.is_file():
        pytest.skip("legacy ts_store.py not found")
    spec = importlib.util.spec_from_file_location("legacy_ts_store", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not getattr(module, "SEGMENTS", None):
        pytest.skip("no segment definitions in the legacy export")
    return module


def _definition(key: str, spec: tuple) -> sg.SegmentDef:
    start, end, kw = spec
    return sg.SegmentDef(
        key=key, name=key, start_lat=start[0], start_lon=start[1], end_lat=end[0],
        end_lon=end[1], tol_m=kw.get("tol", 30), dist_min_m=kw.get("dmin"),
        dist_max_m=kw.get("dmax"), max_s=kw.get("max_s", 1800), uphill=kw.get("uphill", True),
    )  # fmt: skip


@pytest.fixture(scope="module")
def legacy_efforts(ts_store: ModuleType, legacy_dir: Path) -> dict[str, dict[int, list[dict]]]:
    """Per segment: activity id -> legacy efforts, for the activities picked dynamically."""
    files = [
        f
        for f in sorted(legacy_dir.glob("timeseries/year=*/*.parquet"))
        if (legacy_dir / "tracks" / f"{f.stem}.fit").is_file()
    ]
    frames = {f.stem: pd.read_parquet(f, columns=COLUMNS) for f in files}
    found: dict[str, dict[int, list[dict]]] = {}
    for key, (start, end, kw) in ts_store.SEGMENTS.items():
        tol = kw.get("tol", 30)
        hits = {}
        for stem, d in frames.items():
            # the legacy matcher loops over every sample in Python: skip tracks that never pass
            # near both points (same distance function, so nothing it would find is skipped)
            near = d.dropna(subset=["lat"])
            if near.empty or not (
                (ts_store.dist_m(near.lat, near.lon, start) < tol).any()
                and (ts_store.dist_m(near.lat, near.lon, end) < tol).any()
            ):
                continue
            efforts = ts_store.match_segment(d, start, end, **kw)
            if efforts:
                hits[int(stem)] = efforts
        if hits:
            ids = sorted(hits)
            found[key] = {i: hits[i] for i in {pick(ids, w) for w in PICKS}}
    if not found:
        pytest.skip("the legacy matcher finds no efforts")
    return found


def test_matcher_agrees_with_the_legacy_matcher(conn, ts_store, legacy_efforts, legacy_dir,
                                                tmp_path) -> None:  # fmt: skip
    ids = frozenset(i for per in legacy_efforts.values() for i in per)
    summary = legacy.run_import(
        conn, legacy_dir, tmp_path / "data", parts=["activities", "records"],
        selection=legacy.Selection(ids=ids),
    )  # fmt: skip
    assert summary.errors == []
    compared = 0
    for key, per_activity in legacy_efforts.items():
        seg = _definition(key, ts_store.SEGMENTS[key])
        for activity_id, theirs in per_activity.items():
            df = load_records(conn, activity_id, ["lat", "lon", "alt", "dist", "hr"])
            ours = sg.match_segment(df, seg)
            assert len(ours) == len(theirs), (key, activity_id)
            for o, t in zip(ours, theirs, strict=True):
                start = pd.Timestamp(t["start_local"]).tz_localize(ts_store.TZ).tz_convert("UTC")
                assert pd.Timestamp(o["start_utc"]) == start
                assert round(o["duration_s"]) == t["dauer_s"]
                assert round(o["dist_m"]) == pytest.approx(t["dist_m"], abs=1)
                assert round(o["gain_m"]) == pytest.approx(t["hm"], abs=1)
                assert round(o["pace_s_per_km"]) == pytest.approx(t["pace_s_km"], abs=1)
                if t["hf_avg"] is not None:
                    assert round(o["avg_hr"]) == t["hf_avg"]
                    assert o["max_hr"] == t["hf_max"]
                    assert round(o["hr_last60"]) == t["hf_last60"]
                compared += 1
    assert compared > 0
