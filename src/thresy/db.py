"""Postgres connection helpers.

Conventions for every connection returned here:

* ``autocommit=True``. Each statement commits on its own. Group statements with
  ``with conn.transaction(): ...``; the functions in :mod:`thresy.store` do this themselves,
  and nest as savepoints when you call them inside your own ``conn.transaction()`` block.
* Rows are dictionaries (``psycopg.rows.dict_row``) unless you pass another ``row_factory``.
* Timestamps come back as timezone-aware ``datetime`` objects; the session time zone is UTC.

The read/write URL (role ``coach_app``) is ``COACH_DATABASE_URL``; the read-only URL
(role ``coach_ro``) is ``COACH_DATABASE_URL_RO``.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import RowFactory, dict_row

from thresy.config import get_settings

DEFAULT_CONNECT_TIMEOUT_S = 10


def _options(*, read_only: bool, statement_timeout_s: float | None) -> str:
    opts = ["-c TimeZone=UTC"]
    if read_only:
        opts.append("-c default_transaction_read_only=on")
    if statement_timeout_s is not None:
        opts.append(f"-c statement_timeout={int(statement_timeout_s * 1000)}")
    return " ".join(opts)


def database_url(*, read_only: bool = False) -> str:
    """Return the configured URL; raises :class:`thresy.config.ConfigError` when it is unset."""
    return get_settings().require_database_url(read_only=read_only)


def connect(
    url: str | None = None,
    *,
    row_factory: RowFactory[Any] = dict_row,
    autocommit: bool = True,
    statement_timeout_s: float | None = None,
) -> psycopg.Connection[Any]:
    """Open a read/write connection (``COACH_DATABASE_URL`` unless ``url`` is given).

    Use it as a context manager so it is closed: ``with connect() as conn: ...``.
    """
    return psycopg.connect(
        url or database_url(),
        autocommit=autocommit,
        row_factory=row_factory,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
        options=_options(read_only=False, statement_timeout_s=statement_timeout_s),
    )


def connect_ro(
    url: str | None = None,
    *,
    row_factory: RowFactory[Any] = dict_row,
    statement_timeout_s: float | None = 30,
) -> psycopg.Connection[Any]:
    """Open a read-only connection (``COACH_DATABASE_URL_RO`` unless ``url`` is given).

    The session is read-only (``default_transaction_read_only``) and statements time out after
    ``statement_timeout_s`` seconds (``None`` disables the limit). The ``coach_ro`` role has
    SELECT privileges only, so this is defence in depth, not the only barrier.
    """
    return psycopg.connect(
        url or database_url(read_only=True),
        autocommit=True,
        row_factory=row_factory,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
        options=_options(read_only=True, statement_timeout_s=statement_timeout_s),
    )


async def connect_async(
    url: str | None = None,
    *,
    read_only: bool = False,
    row_factory: Any = dict_row,
    statement_timeout_s: float | None = None,
) -> psycopg.AsyncConnection[Any]:
    """Async variant of :func:`connect` / :func:`connect_ro` for asyncio code (the bridge)."""
    return await psycopg.AsyncConnection.connect(
        url or database_url(read_only=read_only),
        autocommit=True,
        row_factory=row_factory,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
        options=_options(read_only=read_only, statement_timeout_s=statement_timeout_s),
    )
