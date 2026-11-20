"""The ``coach`` command line.

Sub-commands are contributed by packages without editing this file: for every name in
:data:`PLUGINS`, the module ``thresy.<name>.cli`` is imported if it exists, and its
``app`` (a ``typer.Typer``) is mounted under the name given in :data:`PLUGINS`. A mount name of
``None`` merges the package's commands and groups into ``coach`` itself (``coach sql``,
``coach send ...``). Packages that do not exist yet are skipped quietly; an import error
*inside* an existing package is not hidden.

Keep plugin modules cheap to import (import pandas, matplotlib, telegram etc. inside the
command functions), because every ``coach`` invocation imports all of them.

Output convention: readable text by default, JSON with ``--json``. Use the helpers::

    from thresy.cli import JsonOption, emit

    @app.command()
    def once(json_out: JsonOption = False) -> None:
        result = {...}
        emit(result, as_json=json_out, text=f"synced {result['n']} activities")
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer

from thresy import logs

# package -> mount name under `coach` (None = merge into the top level)
PLUGINS: dict[str, str | None] = {
    "importer": "import",
    "sync": "sync",
    "analysis": "analysis",
    "bridge": "bridge",
    "tools": None,
    "runner": "runner",
    "workspace": "workspace",
    "backup": "backup",
}

JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


def to_json(data: Any) -> str:
    """Serialise command output (dataclasses, dates, Decimals and Paths included)."""
    return json.dumps(data, default=_json_default, ensure_ascii=False, indent=2)


def emit(data: Any, *, as_json: bool, text: str | None = None) -> None:
    """Print ``data`` as JSON when ``as_json``; otherwise print ``text`` (or ``data``)."""
    if as_json:
        typer.echo(to_json(data))
    else:
        typer.echo(text if text is not None else str(data))


def fail(message: str, *, as_json: bool = False, code: int = 1) -> typer.Exit:
    """Print an error (to stderr, or as ``{"error": ...}`` JSON on stdout) and return an Exit.

    The message is redacted (:func:`thresy.logs.redact`): it often quotes an exception, and a
    connection error can quote a database URL with its password."""
    message = logs.redact(message)
    if as_json:
        typer.echo(to_json({"error": message}))
    else:
        typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


app = typer.Typer(
    name="coach",
    help="Thresy: a self-hosted running coach.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="Database schema and migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log more.")] = False,
) -> None:
    # Every command and service logs through this: request logging of HTTP clients off,
    # secrets redacted from every record and uncaught traceback (thresy.logs).
    logs.configure(verbose=verbose)


@db_app.command("migrate")
def db_migrate(
    json_out: JsonOption = False,
    migrations_dir: Annotated[
        Path | None, typer.Option(help="Directory with NNNN_name.sql files.")
    ] = None,
) -> None:
    """Apply pending migrations (as the read/write role)."""
    from thresy import db, migrate
    from thresy.config import ConfigError

    try:
        with db.connect() as conn:
            applied = migrate.migrate(conn, migrations_dir)
    except (ConfigError, migrate.MigrationError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    data = {"applied": [m.filename for m in applied]}
    if applied:
        text = "\n".join(f"applied {m.filename}" for m in applied)
    else:
        text = "database is up to date"
    emit(data, as_json=json_out, text=text)


@db_app.command("status")
def db_status(
    json_out: JsonOption = False,
    migrations_dir: Annotated[
        Path | None, typer.Option(help="Directory with NNNN_name.sql files.")
    ] = None,
) -> None:
    """Show which migrations are applied, pending, changed or missing."""
    from thresy import db, migrate
    from thresy.config import ConfigError

    try:
        with db.connect() as conn:
            rows = migrate.status(conn, migrations_dir)
    except (ConfigError, migrate.MigrationError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    lines = [
        f"{r.version:04d}_{r.name:<30} {r.state:<8} "
        f"{r.applied_at.isoformat(timespec='seconds') if r.applied_at else ''}".rstrip()
        for r in rows
    ]
    emit(
        [dataclasses.asdict(r) for r in rows],
        as_json=json_out,
        text="\n".join(lines) or "no migrations",
    )


_registered = False


def register_plugins(root: typer.Typer = app) -> list[str]:
    """Mount every existing ``thresy.<name>.cli`` app; returns the package names mounted."""
    mounted = []
    for package, mount in PLUGINS.items():
        module_name = f"thresy.{package}.cli"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in (module_name, f"thresy.{package}"):
                continue  # package not built (yet): skip quietly
            raise
        sub = getattr(module, "app", None)
        if not isinstance(sub, typer.Typer):
            raise TypeError(f"{module_name}.app must be a typer.Typer")
        if mount is None:
            root.registered_commands.extend(sub.registered_commands)
            root.registered_groups.extend(sub.registered_groups)
        else:
            root.add_typer(sub, name=mount)
        mounted.append(package)
    return mounted


def get_app() -> typer.Typer:
    """The ``coach`` app with all plugins mounted (idempotent)."""
    global _registered
    if not _registered:
        register_plugins(app)
        _registered = True
    return app


TERMINAL_FORCING_VARIABLES = ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE")
"""typer renders help and usage errors with rich and makes it treat the output as a terminal,
escape codes included, when GITHUB_ACTIONS, FORCE_COLOR (any value, "0" too) or PY_COLORS is set;
rich does so by itself for TTY_COMPATIBLE=1 (typer/rich_utils.py, rich/console.py)."""


def plain_unless_terminal(stream: Any = None) -> None:
    """Keep help and usage errors free of escape codes unless stdout is a terminal.

    The coaching agent learns the commands from ``coach … --help`` in a non-interactive shell;
    styling there is noise it pays for on every read, and so it is for a pipe or a file. Only
    when one of :data:`TERMINAL_FORCING_VARIABLES` is set does anything need to change (rich
    detects a terminal correctly otherwise), so the usual call does not import rich."""
    if not any(os.environ.get(name) for name in TERMINAL_FORCING_VARIABLES):
        return
    stream = sys.stdout if stream is None else stream
    try:
        terminal = stream.isatty()
    except (AttributeError, ValueError):
        terminal = False
    if not terminal:
        from typer import rich_utils

        rich_utils.FORCE_TERMINAL = False


def main() -> None:
    """Console-script entry point (``coach``)."""
    plain_unless_terminal()
    get_app()()
