"""`coach runner check|run`: the typer plugin for the project's `coach` command.

This shim is the only module of thresy.runner that imports typer (or anything
outside the standard library); `import thresy.runner` never imports it. The
logic lives in thresy.runner.cmdline, which also backs `python -m thresy.runner`.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(
    help="Coding-agent runners (Claude Code, Codex, Cursor).",
    no_args_is_help=True,
)

RunnerOption = Annotated[
    str | None,
    typer.Option("--runner", help="claude, codex, cursor or fake (default: COACH_RUNNER)."),
]


class Tier(StrEnum):
    fast = "fast"
    smart = "smart"


def _config():
    """Runner settings from the environment, with a .env file in the current
    directory filling gaps (like the project's other settings)."""
    from thresy.runner.config import RunnerConfig

    environ: dict[str, str] = {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - python-dotenv comes with pydantic-settings
        pass
    else:
        if Path(".env").is_file():
            environ.update({k: v for k, v in dotenv_values(".env").items() if v is not None})
    environ.update(os.environ)
    return RunnerConfig.from_env(environ)


@app.command()
def check(
    runner: RunnerOption = None,
    all_runners: Annotated[bool, typer.Option("--all", help="Check every runner.")] = False,
    json_out: JsonOption = False,
) -> None:
    """Check that a runner's CLI is installed and logged in (spends no model usage)."""
    import asyncio

    from thresy.runner import cmdline

    try:
        config = _config()
        names = cmdline.runner_names(runner, all_runners, config)
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out, code=2) from exc
    healths = asyncio.run(cmdline.check(names, config))
    data = [cmdline.health_dict(h) for h in healths]
    emit(
        data if all_runners else data[0],
        as_json=json_out,
        text="\n".join(cmdline.health_text(h) for h in healths),
    )
    if not all(h.ok for h in healths):
        raise typer.Exit(1)


@app.command()
def run(
    prompt: Annotated[str, typer.Argument(help='The prompt; "-" reads it from stdin.')],
    cwd: Annotated[Path, typer.Option("--cwd", help="Working directory of the agent.")],
    runner: RunnerOption = None,
    resume: Annotated[
        str | None, typer.Option("--resume", help="Harness session id to resume.")
    ] = None,
    tier: Annotated[Tier, typer.Option("--tier", help="Model tier.")] = Tier.smart,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds.")] = 900.0,
    system_append: Annotated[
        str | None, typer.Option("--system-append", help="Extra system instructions.")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print one JSON object per event (JSON Lines).")
    ] = False,
) -> None:
    """Run a prompt and print events as they arrive (Ctrl+C cancels the run)."""
    import asyncio

    from thresy.runner import cmdline

    try:
        config = _config()
        cmdline.runner_names(runner, False, config)
        req = cmdline.build_request(
            prompt=prompt,
            cwd=cwd,
            resume=resume,
            tier=tier.value,
            timeout=timeout,
            system_append=system_append,
        )
    except ValueError as exc:
        raise fail(str(exc), as_json=json_out, code=2) from exc
    result = asyncio.run(
        cmdline.run_and_print(runner, config, req, as_json=json_out, echo=typer.echo)
    )
    if not result.ok:
        raise typer.Exit(1)
