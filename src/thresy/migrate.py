"""Plain-SQL migrations.

Files are named ``NNNN_name.sql`` (for example ``0001_init.sql``) and live in ``migrations/`` at
the repository root. :func:`migrate` applies the files that are not yet recorded in
``schema_migrations``, in version order, each one in its own transaction together with its
``schema_migrations`` row. A failing file is rolled back completely and stops the run.
A session-level advisory lock keeps two processes from migrating at the same time.

Run migrations as the role that should own the tables (``coach_app``).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import tuple_row

log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
# Arbitrary constant identifying "thresy migrations" in pg_advisory_lock.
ADVISORY_LOCK_ID = 0x5448_5253  # "THRS"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    integer PRIMARY KEY,
  name       text NOT NULL,
  checksum   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migrations directory is inconsistent or a migration failed."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str  # sha256 of the file content

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    name: str
    state: str  # "applied" | "pending" | "changed" (file edited after it was applied) | "missing"
    applied_at: datetime | None


def default_migrations_dir() -> Path:
    """``migrations/`` at the repository root (the package is installed from a checkout)."""
    return Path(__file__).resolve().parents[2] / "migrations"


def discover(directory: Path | None = None) -> list[Migration]:
    """Return the migration files in ``directory``, sorted by version.

    Raises :class:`MigrationError` for files that look like migrations but are misnamed, and
    for duplicate version numbers.
    """
    directory = directory or default_migrations_dir()
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        m = FILENAME_RE.match(path.name)
        if not m:
            raise MigrationError(f"bad migration file name (want NNNN_name.sql): {path.name}")
        version = int(m["version"])
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:04d}: "
                f"{found[version].filename} and {path.name}"
            )
        found[version] = Migration(
            version=version,
            name=m["name"],
            path=path,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return [found[v] for v in sorted(found)]


def _applied(conn: psycopg.Connection[Any]) -> dict[int, tuple[str, str, datetime]]:
    """version -> (name, checksum, applied_at) for every recorded migration."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
        row = cur.fetchone()
        if not row or not row[0]:
            return {}
        cur.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        return {int(v): (name, checksum, at) for v, name, checksum, at in cur.fetchall()}


def status(conn: psycopg.Connection[Any], directory: Path | None = None) -> list[MigrationStatus]:
    """Compare the migration files with ``schema_migrations``."""
    files = {m.version: m for m in discover(directory)}
    applied = _applied(conn)
    out: list[MigrationStatus] = []
    for version in sorted(set(files) | set(applied)):
        f = files.get(version)
        a = applied.get(version)
        if f and not a:
            out.append(MigrationStatus(version, f.name, "pending", None))
        elif a and not f:
            out.append(MigrationStatus(version, a[0], "missing", a[2]))
        else:
            assert f and a
            state = "applied" if a[1] == f.checksum else "changed"
            out.append(MigrationStatus(version, f.name, state, a[2]))
    return out


def migrate(conn: psycopg.Connection[Any], directory: Path | None = None) -> list[Migration]:
    """Apply all pending migrations and return the ones applied (empty when up to date).

    Idempotent. ``conn`` must be in autocommit mode (as returned by :func:`thresy.db.connect`).
    """
    if not conn.autocommit:
        raise MigrationError("migrate() needs an autocommit connection")
    migrations = discover(directory)
    done: list[Migration] = []
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
    try:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        applied = _applied(conn)
        for mig in migrations:
            if mig.version in applied:
                if applied[mig.version][1] != mig.checksum:
                    log.warning(
                        "migration %s was edited after it was applied; edits are not re-run",
                        mig.filename,
                    )
                continue
            sql = mig.path.read_text(encoding="utf-8")
            log.info("applying %s", mig.filename)
            try:
                with conn.transaction():
                    conn.execute(sql)  # type: ignore[arg-type]  # file content, not user input
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum)"
                        " VALUES (%s, %s, %s)",
                        (mig.version, mig.name, mig.checksum),
                    )
            except psycopg.Error as exc:
                raise MigrationError(f"{mig.filename} failed: {exc}") from exc
            done.append(mig)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
    return done
