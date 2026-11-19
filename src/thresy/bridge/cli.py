"""``coach bridge run``: the chat bridge service."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="The chat bridge: chat loop, agent runs, outbox, events.", no_args_is_help=True
)


@app.command("run")
def run() -> None:
    """Run the bridge until SIGTERM or SIGINT (Ctrl+C)."""
    import asyncio

    from thresy.cli import fail
    from thresy.config import ConfigError

    from .main import main

    try:
        asyncio.run(main())
    except ConfigError as exc:
        raise fail(str(exc)) from exc
