"""``coach workspace init``: the typer plugin of the workspace package."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(help="The coach workspace the agent works in.", no_args_is_help=True)


@app.callback()
def _group() -> None:
    """The coach workspace the agent works in."""


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
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="The workspace (default: COACH_WORKSPACE_DIR)."),
    ] = None,
    memory_from: Annotated[
        str | None,
        typer.Option(
            "--memory-from",
            help="Also copy an athlete's memory files into memory/, replacing files of the "
            'same name: a directory, or "-" for a tar archive on stdin.',
        ),
    ] = None,
    json_out: JsonOption = False,
) -> None:
    """Seed an empty workspace from the template; afterwards keep AGENTS.md, CLAUDE.md and
    playbooks/ up to date. memory/, out/, inbox/ and everything the agent or the athlete
    created are never touched (except memory/ with --memory-from)."""
    import tarfile

    from thresy.config import ConfigError, get_settings
    from thresy.workspace import WorkspaceError, import_memory, init_workspace

    try:
        target = workspace or get_settings().workspace_dir
        result = init_workspace(target, template)
        if memory_from is not None:
            source = sys.stdin.buffer if memory_from == "-" else Path(memory_from)
            result.memory_imported = import_memory(result.workspace, source)
    except (WorkspaceError, ConfigError, OSError, tarfile.TarError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    emit(result.as_dict(), as_json=json_out, text=result.text())
    if result.conflicts:
        raise typer.Exit(1)
