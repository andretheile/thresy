"""pg_dump and pg_restore: finding the right version, running them without secrets on the
command line, scratch databases and row counts.

A dump must be restorable into the Postgres major version the stack runs, so pg_dump and
pg_restore must have the server's major version (a newer pg_dump writes statements an older
server rejects). They are looked up in ``COACH_BACKUP_PG_BIN_DIR``, then in
``/usr/lib/postgresql/<major>/bin`` (where Debian's and the PostgreSQL project's packages
install them), then on ``PATH``, and their version is checked.

Connection parameters reach them through libpq's environment variables (``PGHOST``,
``PGPASSWORD``, ...), never through arguments, which every user on the host can read.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


class PgError(RuntimeError):
    """A Postgres tool is missing, has the wrong version, or failed."""


_ENV_KEYS = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "options": "PGOPTIONS",
    "application_name": "PGAPPNAME",
}
_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")


def libpq_env(url: str, *, dbname: str | None = None) -> dict[str, str]:
    """Environment for a libpq tool connecting to ``url`` (and ``dbname`` instead of the
    URL's database). Only what the tools need: PATH, locale and the PG* variables."""
    params = conninfo_to_dict(url)
    unknown = sorted(set(params) - set(_ENV_KEYS))
    if unknown:
        raise PgError(f"connection parameters not supported for pg_dump: {', '.join(unknown)}")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
    for key, value in params.items():
        env[_ENV_KEYS[key]] = str(value)
    if dbname is not None:
        env["PGDATABASE"] = dbname
    return env


def server_major(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT current_setting('server_version_num')::int AS v").fetchone()
    value = row["v"] if isinstance(row, Mapping) else row[0]
    return int(value) // 10000


def server_version(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT current_setting('server_version') AS v").fetchone()
    return str(row["v"] if isinstance(row, Mapping) else row[0])


@dataclass(frozen=True)
class Tool:
    path: str
    version: str
    major: int


def tool_version(path: str) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except OSError as exc:
        raise PgError(f"cannot run {path}: {exc.strerror or exc}") from exc
    match = _VERSION_RE.search(proc.stdout)
    if proc.returncode != 0 or match is None:
        raise PgError(f"cannot tell the version of {path}: {proc.stdout.strip() or proc.stderr}")
    version = match.group(1) + (f".{match.group(2)}" if match.group(2) else "")
    return version, int(match.group(1))


def find_tool(name: str, major: int, bin_dir: Path | None = None) -> Tool:
    """``pg_dump`` or ``pg_restore`` with the server's major version."""
    candidates: list[str] = []
    if bin_dir is not None:
        candidates.append(str(bin_dir / name))
    candidates.append(f"/usr/lib/postgresql/{major}/bin/{name}")
    found = shutil.which(name)
    if found:
        candidates.append(found)
    seen: list[str] = []
    for path in candidates:
        if not (Path(path).is_file() and os.access(path, os.X_OK)):
            continue
        version, tool_major = tool_version(path)
        if tool_major == major:
            return Tool(path, version, tool_major)
        seen.append(f"{path} is {version}")
    detail = "; ".join(seen) if seen else "none found"
    raise PgError(
        f"{name} for PostgreSQL {major} is needed (the server's major version) but {detail}. "
        f"Install postgresql-client-{major} or set COACH_BACKUP_PG_BIN_DIR."
    )


def _run(argv: list[str], env: Mapping[str, str], what: str) -> None:
    try:
        proc = subprocess.run(
            argv, env=dict(env), capture_output=True, text=True, timeout=6 * 3600, check=False
        )
    except OSError as exc:
        raise PgError(f"cannot run {argv[0]}: {exc.strerror or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PgError(f"{what} did not finish within {exc.timeout:g} s") from exc
    if proc.returncode != 0:
        raise PgError(f"{what} failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")


def dump(tool: Tool, url: str, target: Path, *, snapshot: str | None = None) -> None:
    """``pg_dump --format=custom`` of the URL's database into ``target``."""
    argv = [tool.path, "--format=custom", "--no-password", f"--file={target}"]
    if snapshot:
        argv.append(f"--snapshot={snapshot}")
    _run(argv, libpq_env(url), "pg_dump")


def restore(tool: Tool, url: str, dbname: str, source: Path, *, privileges: bool = True) -> None:
    """Restore a custom-format dump into the (empty) database ``dbname`` on the server of
    ``url``, in one transaction, stopping at the first error. Objects belong to the
    connecting role (``--no-owner``)."""
    argv = [
        tool.path,
        "--no-owner",
        "--exit-on-error",
        "--single-transaction",
        "--no-password",
        f"--dbname={dbname}",
    ]
    if not privileges:
        argv.append("--no-privileges")
    argv.append(str(source))
    _run(argv, libpq_env(url, dbname=dbname), "pg_restore")


def tables(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    return [r["tablename"] if isinstance(r, Mapping) else r[0] for r in rows]


def row_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Rows per table of the public schema."""
    counts = {}
    for table in tables(conn):
        row = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
        ).fetchone()
        counts[table] = int(row["n"] if isinstance(row, Mapping) else row[0])
    return counts


def create_database(conn: psycopg.Connection[Any], name: str) -> None:
    try:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege as exc:
        raise PgError(
            "the backup's database role may not create the scratch database for the restore "
            "check; give it CREATEDB (ALTER ROLE coach_app CREATEDB, see docs/backup.md)"
        ) from exc


def drop_database(conn: psycopg.Connection[Any], name: str) -> None:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def with_dbname(url: str, dbname: str) -> str:
    params = conninfo_to_dict(url)
    params["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**params)
