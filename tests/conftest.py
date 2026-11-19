"""Shared pytest fixtures.

Database tests run against the development Postgres (``compose.dev.yml``). Once per test
session a fresh database with a unique name is created, migrated as ``coach_app`` and dropped
at the end. Tests that need it skip with a clear message when the server is not reachable.

Environment:

* ``COACH_TEST_ADMIN_URL``: superuser URL of the dev server
  (default ``postgresql://postgres:postgres@127.0.0.1:55432/postgres``).
* ``COACH_TEST_APP_PASSWORD`` / ``COACH_TEST_RO_PASSWORD``: passwords of ``coach_app`` /
  ``coach_ro`` on that server (default: the compose.dev.yml values). Missing roles are created
  with these passwords.
* ``COACH_LEGACY_DIR``: the old bot's export, for the ``legacy`` parity tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from thresy import db, migrate

DEFAULT_ADMIN_URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
START_HINT = "start it with: docker compose -f compose.dev.yml up -d"


@dataclass(frozen=True)
class PgServer:
    admin_url: str
    app_password: str
    ro_password: str

    def url(self, *, user: str, password: str, dbname: str) -> str:
        params = conninfo_to_dict(self.admin_url)
        params.update(user=user, password=password, dbname=dbname)
        return make_conninfo(**params)

    def admin(self, dbname: str | None = None) -> psycopg.Connection[Any]:
        params = conninfo_to_dict(self.admin_url)
        if dbname:
            params["dbname"] = dbname
        return psycopg.connect(make_conninfo(**params), autocommit=True, connect_timeout=5)

    def create_database(self) -> TestDatabase:
        name = f"thresy_test_{uuid.uuid4().hex[:12]}"
        with self.admin() as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER coach_app").format(sql.Identifier(name)))
        return TestDatabase(
            name=name,
            app_url=self.url(user="coach_app", password=self.app_password, dbname=name),
            ro_url=self.url(user="coach_ro", password=self.ro_password, dbname=name),
        )

    def drop_database(self, name: str) -> None:
        with self.admin() as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@dataclass(frozen=True)
class TestDatabase:
    __test__ = False  # not a test class

    name: str
    app_url: str
    ro_url: str


def _ensure_roles(server: PgServer) -> None:
    """Create coach_app / coach_ro like docker/postgres/initdb/10-roles.sh, if missing."""
    with server.admin() as conn:
        for role, password in (
            ("coach_app", server.app_password),
            ("coach_ro", server.ro_password),
        ):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        conn.execute("ALTER ROLE coach_ro SET default_transaction_read_only = on")


@pytest.fixture(scope="session")
def pg_server() -> PgServer:
    server = PgServer(
        admin_url=os.environ.get("COACH_TEST_ADMIN_URL", DEFAULT_ADMIN_URL),
        app_password=os.environ.get("COACH_TEST_APP_PASSWORD", "coach_app"),
        ro_password=os.environ.get("COACH_TEST_RO_PASSWORD", "coach_ro"),
    )
    try:
        with server.admin() as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        pytest.skip(f"dev database not reachable ({reason}); {START_HINT}")
    _ensure_roles(server)
    return server


@pytest.fixture(scope="session")
def test_db(pg_server: PgServer) -> Iterator[TestDatabase]:
    """A migrated database for the whole session, dropped afterwards."""
    database = pg_server.create_database()
    try:
        with db.connect(database.app_url) as conn:
            migrate.migrate(conn)
        yield database
    finally:
        pg_server.drop_database(database.name)


def _truncate_all(conn: psycopg.Connection[Any]) -> None:
    tables = [
        r["tablename"]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename <> 'schema_migrations'"
        ).fetchall()
    ]
    if tables:
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in tables)
            )
        )


@pytest.fixture
def conn(test_db: TestDatabase) -> Iterator[psycopg.Connection[Any]]:
    """A read/write (coach_app) connection to an emptied test database."""
    with db.connect(test_db.app_url) as connection:
        _truncate_all(connection)
        yield connection


@pytest.fixture
def ro_conn(test_db: TestDatabase) -> Iterator[psycopg.Connection[Any]]:
    """A read-only (coach_ro) connection to the test database."""
    with db.connect_ro(test_db.ro_url) as connection:
        yield connection


@pytest.fixture
def fresh_db(pg_server: PgServer) -> Iterator[TestDatabase]:
    """An empty, unmigrated database for one test."""
    database = pg_server.create_database()
    try:
        yield database
    finally:
        pg_server.drop_database(database.name)


@pytest.fixture(scope="session")
def legacy_dir() -> Path:
    """The old bot's export (``COACH_LEGACY_DIR``); skips when unset or missing."""
    value = os.environ.get("COACH_LEGACY_DIR")
    if not value:
        pytest.skip("COACH_LEGACY_DIR is not set")
    path = Path(value).expanduser()
    if not path.is_dir():
        pytest.skip(f"COACH_LEGACY_DIR does not exist: {path}")
    return path
