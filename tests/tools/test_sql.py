"""coach sql: read-only role, table and JSON output, row limit."""

from __future__ import annotations

import json

import pytest

from tests import synthetic
from thresy import store
from thresy.tools import sql

from .test_send import coach

pytestmark = pytest.mark.db


@pytest.fixture
def ro_env(test_db, tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    return {"COACH_DATABASE_URL_RO": test_db.ro_url}


def _activities(conn, n: int) -> None:
    for i in range(n):
        store.upsert_activity(
            conn,
            synthetic.activity(1000 + i, start_local=f"2030-01-{i + 1:02d} 07:00:00"),
            source="test",
        )


def test_table_output(conn, ro_env) -> None:
    _activities(conn, 2)
    result = coach(
        ["sql", "SELECT id, distance_km, pace, raw->'activityType' AS t FROM runs ORDER BY id;"],
        ro_env,
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split(" | ")[0].strip() == "id"
    assert lines[1].startswith("-") and "-+-" in lines[1]
    assert lines[2].startswith("1000 | 10")
    assert '{"typeId":1' in lines[2]
    assert lines[-1] == "(2 rows)"


def test_json_output_and_truncation(conn, ro_env) -> None:
    _activities(conn, 5)
    result = coach(
        ["sql", "SELECT id, start_utc FROM activities ORDER BY id", "-n", "3", "--json"], ro_env
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["columns"] == ["id", "start_utc"]
    assert [r["id"] for r in data["rows"]] == [1000, 1001, 1002]
    assert data["truncated"] is True and data["row_count"] == 3
    assert data["rows"][0]["start_utc"] == "2030-01-02T06:00:00+00:00"
    text = coach(["sql", "SELECT id FROM activities", "--limit", "2"], ro_env).output
    assert "showing the first 2 rows; the query returned more" in text


def test_statements_without_a_cursor(conn, ro_env) -> None:
    result = coach(["sql", "SHOW TimeZone"], ro_env)
    assert result.exit_code == 0 and "UTC" in result.output
    result = coach(["sql", "EXPLAIN SELECT 1"], ro_env)
    assert result.exit_code == 0 and "QUERY PLAN" in result.output


def test_writes_are_refused(conn, ro_env) -> None:
    result = coach(["sql", "DELETE FROM activities"], ro_env)
    assert result.exit_code == 1
    assert "read-only" in result.output or "permission denied" in result.output
    result = coach(["sql", "SELEC nonsense"], ro_env)
    assert result.exit_code == 1 and "syntax error" in result.output


def test_statement_timeout(conn, ro_env) -> None:
    result = coach(["sql", "SELECT pg_sleep(3)", "--timeout", "1"], ro_env)
    assert result.exit_code == 1 and "statement timeout" in result.output


def test_missing_url(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = coach(["sql", "SELECT 1"], {})
    assert result.exit_code == 1 and "COACH_DATABASE_URL_RO" in result.output


def test_run_query_directly(ro_conn) -> None:
    result = sql.run_query(ro_conn, "SELECT generate_series(1, 10) AS n", limit=4)
    assert [r["n"] for r in result.rows] == [1, 2, 3, 4] and result.truncated
    assert "(showing the first 4 rows" in sql.as_text(result)
    with pytest.raises(ValueError):
        sql.run_query(ro_conn, " ; ", limit=1)
