from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from thresy import db, migrate

pytestmark = pytest.mark.db

CONTRACT_TABLES = {
    "activities", "activity_records", "activity_laps", "wellness_daily", "hrv_readings",
    "sleep_levels", "personal_records", "segments", "segment_efforts", "races",
    "planned_workouts", "agent_sessions", "agent_runs", "messages", "outbox", "events",
    "sync_state", "schema_migrations",
}  # fmt: skip


def test_discover_real_migrations() -> None:
    migrations = migrate.discover()
    assert migrations[0].version == 1 and migrations[0].name == "init"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)


def test_discover_rejects_bad_names(tmp_path: Path) -> None:
    (tmp_path / "01_bad.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="bad migration file name"):
        migrate.discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.discover(tmp_path)


def test_session_database_has_contract_schema(conn) -> None:
    tables = {
        r["tablename"]
        for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert tables >= CONTRACT_TABLES
    views = {r["viewname"] for r in conn.execute("SELECT viewname FROM pg_views")}
    assert "runs" in views
    owners = {
        r["tableowner"]
        for r in conn.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'public'")
    }
    assert owners == {"coach_app"}


def test_migrate_is_idempotent(conn) -> None:
    before = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert migrate.migrate(conn) == []
    assert migrate.migrate(conn) == []
    after = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert after == before
    assert all(s.state == "applied" for s in migrate.status(conn))


def test_fresh_database_migrates_and_reports_status(fresh_db, tmp_path: Path) -> None:
    (tmp_path / "0001_one.sql").write_text("CREATE TABLE one (id int);")
    (tmp_path / "0002_two.sql").write_text("CREATE TABLE two (id int);")
    with db.connect(fresh_db.app_url) as conn:
        assert [s.state for s in migrate.status(conn, tmp_path)] == ["pending", "pending"]
        assert [m.version for m in migrate.migrate(conn, tmp_path)] == [1, 2]
        assert migrate.migrate(conn, tmp_path) == []

        (tmp_path / "0002_two.sql").write_text("CREATE TABLE two (id int, x int);")
        (tmp_path / "0001_one.sql").unlink()
        (tmp_path / "0003_three.sql").write_text("CREATE TABLE three (id int);")
        states = {s.version: s.state for s in migrate.status(conn, tmp_path)}
        assert states == {1: "missing", 2: "changed", 3: "pending"}


def test_failing_migration_is_rolled_back(fresh_db, tmp_path: Path) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok (id int);")
    (tmp_path / "0002_broken.sql").write_text(
        "CREATE TABLE half (id int);\nINSERT INTO nowhere VALUES (1);"
    )
    with db.connect(fresh_db.app_url) as conn:
        with pytest.raises(migrate.MigrationError, match=r"0002_broken\.sql"):
            migrate.migrate(conn, tmp_path)
        applied = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert applied == [1]
        exists = conn.execute("SELECT to_regclass('half') IS NOT NULL AS e").fetchone()["e"]
        assert exists is False  # the whole file was rolled back

        (tmp_path / "0002_broken.sql").write_text("CREATE TABLE half (id int);")
        assert [m.version for m in migrate.migrate(conn, tmp_path)] == [2]


def test_migrate_needs_autocommit(fresh_db) -> None:
    with (
        db.connect(fresh_db.app_url, autocommit=False) as conn,
        pytest.raises(migrate.MigrationError, match="autocommit"),
    ):
        migrate.migrate(conn)


def test_contract_migration_on_fresh_database(fresh_db) -> None:
    with db.connect(fresh_db.app_url) as conn:
        assert [m.version for m in migrate.migrate(conn)] == [m.version for m in migrate.discover()]
        assert migrate.migrate(conn) == []
    # the ro role can read the new database right away
    with db.connect_ro(fresh_db.ro_url) as ro:
        assert ro.execute("SELECT count(*) AS n FROM activities").fetchone()["n"] == 0
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("INSERT INTO sync_state (key, value) VALUES ('k', '1')")
