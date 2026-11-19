from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.analysis.synth import (
    M_PER_DEG,
    ORIGIN,
    T0,
    Phase,
    frame,
    hill_out_and_back,
    put_activity,
    records,
)
from thresy import db
from thresy.analysis import segments as sg

BOTTOM = (ORIGIN[0] + 501 / M_PER_DEG, ORIGIN[1])  # foot of the synthetic hill
TOP = (ORIGIN[0] + 1501 / M_PER_DEG, ORIGIN[1])  # its top

HILL = sg.SegmentDef(
    key="hill", name="Synthetic hill", start_lat=BOTTOM[0], start_lon=BOTTOM[1],
    end_lat=TOP[0], end_lon=TOP[1], tol_m=30, dist_min_m=800, dist_max_m=1200,
)  # fmt: skip


def test_match_segment_finds_each_uphill_pass() -> None:
    df = frame(records(hill_out_and_back(laps=2)))
    efforts = sg.match_segment(df, HILL)
    assert len(efforts) == 2
    for e in efforts:
        assert e["dist_m"] == pytest.approx(1000, abs=60)
        assert e["gain_m"] == pytest.approx(80, abs=6)
        assert e["duration_s"] == pytest.approx(400, abs=25)
        assert e["pace_s_per_km"] == pytest.approx(400, abs=15)
        assert 140 <= e["avg_hr"] <= 160 and e["max_hr"] <= 160
        assert e["hr_last60"] > e["avg_hr"]  # the heart rate rises over the climb
    assert efforts[1]["start_utc"] - efforts[0]["start_utc"] > timedelta(minutes=15)


def test_match_segment_direction_distance_and_time_limits() -> None:
    df = frame(records(hill_out_and_back()))
    down = sg.SegmentDef("down", "down", TOP[0], TOP[1], BOTTOM[0], BOTTOM[1], dist_min_m=800)
    assert sg.match_segment(df, down) == []  # uphill required, but it goes down
    from dataclasses import replace

    efforts = sg.match_segment(df, replace(down, uphill=False))
    assert len(efforts) == 1 and efforts[0]["gain_m"] < -70
    assert sg.match_segment(df, replace(HILL, dist_max_m=900)) == []
    assert sg.match_segment(df, replace(HILL, max_s=200)) == []
    assert sg.match_segment(df.iloc[:0], HILL) == []


def test_segment_files(tmp_path) -> None:
    toml = tmp_path / "segments.toml"
    toml.write_text(
        '[[segments]]\nkey = "hill"\nname = "Hill"\nstart = [10.0, 20.0]\nend = [10.01, 20.0]\n'
        'dist_min_m = 800\n\n[[segments]]\nkey = "flat"\nstart_lat = 1.0\nstart_lon = 2.0\n'
        "end_lat = 1.01\nend_lon = 2.0\nuphill = false\n"
    )
    segs = sg.load_segment_file(toml)
    assert [s.key for s in segs] == ["hill", "flat"]
    assert segs[0].start_lat == 10.0 and segs[0].dist_min_m == 800 and segs[1].name == "flat"
    js = tmp_path / "segments.json"
    js.write_text(json.dumps([{"key": "x", "start": [1, 2], "end": [1.01, 2]}]))
    assert sg.load_segment_file(js)[0].tol_m == 30
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"segments": [{"key": "x", "start": [1, 2]}]}))
    with pytest.raises(ValueError, match="missing"):
        sg.load_segment_file(bad)
    bad.write_text(json.dumps([{"key": "x", "start": [1, 2], "end": [1, 2], "colour": "red"}]))
    with pytest.raises(ValueError, match="unknown"):
        sg.load_segment_file(bad)


@pytest.mark.db
def test_efforts_are_cached_and_definitions_invalidate(conn) -> None:
    put_activity(conn, 201, records(hill_out_and_back(laps=2)))
    put_activity(conn, 202, records(hill_out_and_back()), t0=T0 + timedelta(days=30))
    elsewhere = [Phase(600, speed=3.0)]
    put_activity(conn, 203, records(elsewhere, origin=(11.0, 21.0)), t0=T0 + timedelta(days=31))
    assert sg.upsert_segments(conn, [HILL]) == {"new": 1, "updated": 0, "unchanged": 0}

    first = sg.segment_efforts(conn, HILL)
    assert len(first["efforts"]) == 3 and first["cache"] == "written"
    assert first["candidates"] == 2 and first["scanned"] == 2
    second = sg.segment_efforts(conn, HILL)
    assert second["scanned"] == 0 and second["cached"] == 2
    assert [e["duration_s"] for e in second["efforts"]] == pytest.approx(
        [e["duration_s"] for e in first["efforts"]]
    )
    assert sg.upsert_segments(conn, [HILL]) == {"new": 0, "updated": 0, "unchanged": 1}
    from dataclasses import replace

    sg.upsert_segments(conn, [replace(HILL, tol_m=25)])
    assert conn.execute("SELECT count(*) AS n FROM segment_efforts").fetchone()["n"] == 0

    hist = sg.history(conn, HILL, sg.segment_efforts(conn, HILL)["efforts"])
    assert hist["count"] == 3 and hist["activities"] == 2
    assert hist["best"]["duration_s"] == min(e["duration_s"] for e in hist["efforts"])
    assert hist["efforts"][0]["start_local"] == hist["efforts"][0]["start_utc"].replace(
        tzinfo=None
    ) + timedelta(hours=1)
    assert hist["fit"] is None  # too few efforts for a pace-heart-rate fit
    assert hist["per_year"][0]["efforts"] == 3


@pytest.mark.db
def test_agent_role_writes_the_cache_and_read_only_degrades(conn, test_db) -> None:
    put_activity(conn, 211, records(hill_out_and_back()))
    sg.upsert_segments(conn, [HILL])
    with db.connect_ro(test_db.ro_url) as ro:
        found = sg.segment_efforts(conn=ro, seg=HILL)
    assert found["cache"] == "read-only" and len(found["efforts"]) == 1
    assert conn.execute("SELECT count(*) AS n FROM segment_efforts").fetchone()["n"] == 0
    with db.connect(test_db.agent_url) as agent:
        found = sg.segment_efforts(agent, HILL)
    assert found["cache"] == "written"
    assert conn.execute("SELECT count(*) AS n FROM segment_efforts").fetchone()["n"] == 1


def test_pace_at_heart_rate_fit() -> None:
    rows = [
        {"avg_hr": hr, "pace_s_per_km": 600 - 2 * hr, "duration_s": 400.0}
        for hr in (140, 145, 150, 155, 160)
    ]
    fit = sg._pace_hr_fit(rows)
    assert fit["slope_s_per_bpm"] == pytest.approx(-2.0) and fit["r2"] == pytest.approx(1.0)
    rows[0]["pace_s_per_km"] = 100  # noisy but still negative
    assert sg._pace_hr_fit(rows[:4]) is None  # fewer than five efforts
