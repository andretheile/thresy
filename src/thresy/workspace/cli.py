"""``coach workspace init|scripts``: the typer plugin of the workspace package."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Annotated

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(help="The coach workspace the agent works in.", no_args_is_help=True)

WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", help="The workspace (default: COACH_WORKSPACE_DIR)."),
]


@app.callback()
def _group() -> None:
    """The coach workspace the agent works in."""


def _source(value: str) -> Path | IO[bytes]:
    return sys.stdin.buffer if value == "-" else Path(value)


@app.command("init")
def init(
    template: Annotated[
        Path | None,
        typer.Option(
            "--template",
            help="Template directory (default: COACH_WORKSPACE_TEMPLATE, else the repository's "
            "workspace/).",
        ),
    ] = None,
    workspace: WorkspaceOption = None,
    memory_from: Annotated[
        str | None,
        typer.Option(
            "--memory-from",
            help="Also copy an athlete's memory files into memory/, replacing files of the "
            'same name: a directory, or "-" for a tar archive on stdin.',
        ),
    ] = None,
    scripts_from: Annotated[
        str | None,
        typer.Option(
            "--scripts-from",
            help="Also copy kept scripts into scripts/ (restoring a backup), replacing files "
            'of the same name: a directory, or "-" for a tar archive on stdin.',
        ),
    ] = None,
    json_out: JsonOption = False,
) -> None:
    """Seed an empty workspace from the template; afterwards keep AGENTS.md, CLAUDE.md and
    playbooks/ up to date. memory/, scripts/, out/, inbox/ and everything the agent or the
    athlete created are never touched (except memory/ with --memory-from and scripts/ with
    --scripts-from); out/, inbox/ and scripts/ are created when missing."""
    import tarfile

    from thresy.config import ConfigError, get_settings
    from thresy.workspace import WorkspaceError, import_memory, import_scripts, init_workspace

    if memory_from == "-" and scripts_from == "-":
        raise fail("only one of --memory-from and --scripts-from can read stdin", as_json=json_out)
    try:
        target = workspace or get_settings().workspace_dir
        result = init_workspace(target, template)
        if memory_from is not None:
            result.memory_imported = import_memory(result.workspace, _source(memory_from))
        if scripts_from is not None:
            result.scripts_imported = import_scripts(result.workspace, _source(scripts_from))
    except (WorkspaceError, ConfigError, OSError, tarfile.TarError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    emit(result.as_dict(), as_json=json_out, text=result.text())
    if result.conflicts:
        raise typer.Exit(1)


@app.command("scripts")
def scripts(workspace: WorkspaceOption = None, json_out: JsonOption = False) -> None:
    """The analysis scripts kept in scripts/: one entry per script, read from its header (the
    question it answers, how to call it, whether it is marked as a candidate for a built-in
    command) plus any problem (no header, no Usage: line, does not parse). Run it before you
    write a script; playbooks/scripts.md describes the header and when to keep a script."""
    from thresy.config import ConfigError, get_settings
    from thresy.workspace import index_scripts

    try:
        target = workspace or get_settings().workspace_dir
    except ConfigError as exc:
        raise fail(str(exc), as_json=json_out) from exc
    index = index_scripts(target)
    emit(index.as_dict(), as_json=json_out, text=index.text())
