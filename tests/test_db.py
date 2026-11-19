"""Connection helpers and the read-only role."""

from __future__ import annotations

from datetime import UTC

import psycopg
import pytest

from thresy import db
from thresy.config import ConfigError, get_settings

pytestmark = pytest.mark.db

INSERT_RUN = (
    "INSERT INTO activities (id, start_utc, local_date, type, is_run, source)"
    " VALUES (1, '2030-01-02 06:00:00+00', '2030-01-02', 'running', true, 'test')"
)

WRITES = [
    "INSERT INTO sync_state (key, value) VALUES ('k', '1')",
    "UPDATE activities SET name = 'x'",
    "DELETE FROM activities",
    "TRUNCATE events",
    "CREATE TABLE intruder (id int)",
    "DROP TABLE activities",
    "ALTER TABLE activities ADD COLUMN x int",
]


def test_connect_defaults(conn) -> None:
    assert conn.autocommit is True
    row = conn.execute("SELECT current_user AS u, current_setting('TimeZone') AS tz").fetchone()
    assert row == {"u": "coach_app", "tz": "UTC"}
    conn.execute(INSERT_RUN)
    start = conn.execute("SELECT start_utc FROM activities").fetchone()["start_utc"]
    assert start.tzinfo is not None and start.utcoffset() == UTC.utcoffset(start)


def test_ro_can_read(conn, ro_conn) -> None:
    conn.execute(INSERT_RUN)
    row = ro_conn.execute("SELECT current_user AS u, count(*) AS n FROM runs").fetchone()
    assert row == {"u": "coach_ro", "n": 1}
    assert ro_conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] >= 1


@pytest.mark.parametrize("statement", WRITES)
def test_ro_cannot_write_in_default_session(ro_conn, statement) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        ro_conn.execute(statement)


@pytest.mark.parametrize("statement", WRITES)
def test_ro_cannot_write_even_after_lifting_read_only(test_db, statement) -> None:
    """The privileges themselves forbid writing, not only the read-only session setting."""
    with psycopg.connect(test_db.ro_url, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        conn.execute("SET transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_ro_has_no_write_privileges_on_any_table(test_db) -> None:
    with psycopg.connect(test_db.ro_url) as conn:
        rows = conn.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
            " WHERE grantee = 'coach_ro' AND privilege_type <> 'SELECT'"
        ).fetchall()
    assert rows == []


def test_missing_url_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("COACH_DATABASE_URL", raising=False)
    monkeypatch.chdir("/")  # no .env here
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError, match="COACH_DATABASE_URL"):
            db.database_url()
    finally:
        get_settings.cache_clear()
