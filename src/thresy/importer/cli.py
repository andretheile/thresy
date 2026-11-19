"""``coach import legacy``: the typer plugin of the importer."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(help="Import data from other sources.", no_args_is_help=True)


class Part(StrEnum):
    activities = "activities"
    records = "records"
    wellness = "wellness"
    prs = "prs"


@app.command("legacy")
def legacy(
    legacy_dir: Annotated[
        Path | None,
        typer.Option("--dir", help="The old bot's export (default: COACH_LEGACY_DIR)."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Where FIT copies go (default: COACH_DATA_DIR)."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=0, help="Only the N most recent activities (after filters)."),
    ] = None,
    only: Annotated[
        list[Part] | None,
        typer.Option(
            "--only",
            help="Import only these parts (repeatable): activities (summaries), records "
            "(FIT files: per-second records and laps), wellness (daily reports), prs "
            "(personal records). Default: all.",
        ),
    ] = None,
    ids: Annotated[
        list[int] | None,
        typer.Option("--id", help="Only this activity id (repeatable)."),
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Only activities on or after YYYY-MM-DD.")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Only activities on or before YYYY-MM-DD.")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="No progress output.")] = False,
    json_out: JsonOption = False,
) -> None:
    """Import the old bot's export: activities, FIT records, wellness, personal records.

    Idempotent and resumable: run it again after an interruption, and a second complete run
    changes nothing. Raises no events. Progress goes to stderr, the summary to stdout.
    """
    from thresy import db
    from thresy.config import ConfigError, get_settings
    from thresy.importer.legacy import PARTS, Selection, run_import

    settings = get_settings()
    source = legacy_dir or settings.legacy_dir
    if source is None:
        raise fail("no legacy directory: pass --dir or set COACH_LEGACY_DIR", as_json=json_out)
    try:
        selection = Selection(
            ids=frozenset(ids) if ids else None,
            since=date.fromisoformat(since) if since else None,
            until=date.fromisoformat(until) if until else None,
            limit=limit,
        )
    except ValueError as exc:
        raise fail(f"bad date: {exc}", as_json=json_out, code=2) from exc

    def progress(message: str) -> None:
        if not quiet:
            typer.echo(message, err=True)

    try:
        with db.connect() as conn:
            summary = run_import(
                conn,
                source,
                data_dir or settings.data_dir,
                parts=[p.value for p in only] if only else PARTS,
                selection=selection,
                progress=progress,
            )
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    emit(summary.as_dict(), as_json=json_out, text=summary.text())
    if summary.errors:
        raise typer.Exit(1)
