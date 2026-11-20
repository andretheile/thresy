from __future__ import annotations

import json
import sys
import types

import pytest
import typer
from typer.testing import CliRunner

from thresy import cli

runner = CliRunner()


@pytest.fixture
def fake_plugins(monkeypatch):
    """Install fake thresy.sync.cli and thresy.tools.cli modules."""
    sync_app = typer.Typer()

    @sync_app.command()
    def once(json_out: cli.JsonOption = False) -> None:
        cli.emit({"synced": 2}, as_json=json_out, text="synced 2")

    @sync_app.command()
    def poll() -> None:
        typer.echo("polling")

    tools_app = typer.Typer()

    @tools_app.command()
    def sql(query: str) -> None:
        typer.echo(f"ran {query}")

    send_app = typer.Typer()

    @send_app.command("text")
    def send_text(message: str) -> None:
        typer.echo(f"sent {message}")

    tools_app.add_typer(send_app, name="send")

    for name, sub in (("sync", sync_app), ("tools", tools_app)):
        pkg = types.ModuleType(f"thresy.{name}")
        pkg.__path__ = []  # mark as package
        mod = types.ModuleType(f"thresy.{name}.cli")
        mod.app = sub  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, f"thresy.{name}", pkg)
        monkeypatch.setitem(sys.modules, f"thresy.{name}.cli", mod)
    yield


def _fresh_root() -> typer.Typer:
    root = typer.Typer(no_args_is_help=True)

    @root.command()
    def version() -> None:
        typer.echo("0")

    return root


def test_plugins_are_discovered_and_missing_ones_skipped(fake_plugins, monkeypatch) -> None:
    # Independent of which real packages exist: two fakes and one package that does not exist.
    monkeypatch.setattr(cli, "PLUGINS", {"sync": "sync", "nosuchpackage": "x", "tools": None})
    root = _fresh_root()
    mounted = cli.register_plugins(root)
    assert mounted == ["sync", "tools"]

    result = runner.invoke(root, ["sync", "once"])
    assert result.exit_code == 0 and result.output.strip() == "synced 2"
    result = runner.invoke(root, ["sync", "once", "--json"])
    assert json.loads(result.output) == {"synced": 2}
    # tools is merged into the top level
    assert runner.invoke(root, ["sql", "select 1"]).output.strip() == "ran select 1"
    assert runner.invoke(root, ["send", "text", "hi"]).output.strip() == "sent hi"


def test_broken_plugin_is_not_hidden(monkeypatch) -> None:
    pkg = types.ModuleType("thresy.bridge")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "thresy.bridge", pkg)
    bad = types.ModuleType("thresy.bridge.cli")
    bad.app = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thresy.bridge.cli", bad)
    with pytest.raises(TypeError, match=r"typer\.Typer"):
        cli.register_plugins(_fresh_root())


def test_help_lists_db_commands() -> None:
    result = runner.invoke(cli.get_app(), ["--help"])
    assert result.exit_code == 0
    assert "db" in result.output
    result = runner.invoke(cli.get_app(), ["db", "--help"])
    assert "migrate" in result.output and "status" in result.output


def test_emit_and_to_json(capsys) -> None:
    from datetime import date

    cli.emit({"d": date(2030, 1, 2)}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"d": "2030-01-02"}
    cli.emit({"d": 1}, as_json=False, text="hello")
    assert capsys.readouterr().out == "hello\n"


@pytest.mark.db
def test_db_commands_against_test_database(test_db, monkeypatch) -> None:
    from thresy.config import get_settings

    monkeypatch.setenv("COACH_DATABASE_URL", test_db.app_url)
    get_settings.cache_clear()
    try:
        result = runner.invoke(cli.get_app(), ["db", "migrate", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"applied": []}
        result = runner.invoke(cli.get_app(), ["db", "status"])
        assert result.exit_code == 0 and "0001_init" in result.output
        assert "applied" in result.output
        result = runner.invoke(cli.get_app(), ["db", "status", "--json"])
        assert json.loads(result.output)[0]["state"] == "applied"
    finally:
        get_settings.cache_clear()


def test_db_migrate_without_url_fails_cleanly(monkeypatch, tmp_path) -> None:
    from thresy.config import get_settings

    monkeypatch.delenv("COACH_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env
    get_settings.cache_clear()
    try:
        result = runner.invoke(cli.get_app(), ["db", "migrate", "--json"])
        assert result.exit_code == 1
        assert "COACH_DATABASE_URL" in json.loads(result.output)["error"]
    finally:
        get_settings.cache_clear()


# -- plain help for the agent and for pipes -------------------------------------------------------

FORCING = {"GITHUB_ACTIONS": "true", "FORCE_COLOR": "0", "PY_COLORS": "1", "TTY_COMPATIBLE": "1"}


def _coach_output(args: list[str], env: dict[str, str], *, terminal: bool) -> str:
    """Run ``coach`` in a fresh interpreter (typer reads the environment at import) with stdout
    on a pipe or on a pseudo-terminal."""
    import os
    import pty
    import subprocess

    argv = [sys.executable, "-c", "from thresy.cli import main; main()", *args]
    env = {**os.environ, **env}
    if not terminal:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        return proc.stdout
    leader, follower = pty.openpty()
    proc = subprocess.Popen(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=follower, stderr=subprocess.DEVNULL
    )
    os.close(follower)
    chunks = []
    try:
        while True:
            try:
                data = os.read(leader, 65536)
            except OSError:  # Linux: EIO once the child has closed the terminal
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(leader)
        proc.wait(timeout=60)
    return b"".join(chunks).decode("utf-8", "replace")


@pytest.mark.parametrize("name", sorted(FORCING))
def test_help_on_a_pipe_is_plain_whatever_forces_a_terminal(name) -> None:
    out = _coach_output(["sql", "--help"], {name: FORCING[name]}, terminal=False)
    assert "\x1b" not in out
    assert "wake_date" in out and "--limit" in out


def test_help_on_a_terminal_keeps_its_styling() -> None:
    out = _coach_output(["sql", "--help"], FORCING, terminal=True)
    assert "\x1b[" in out


def test_plain_unless_terminal_leaves_rich_alone_without_a_forcing_variable(monkeypatch) -> None:
    from typer import rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", None)
    cli.plain_unless_terminal()  # the autouse fixture removed every forcing variable
    assert rich_utils.FORCE_TERMINAL is None
    monkeypatch.setenv("FORCE_COLOR", "1")
    cli.plain_unless_terminal(types.SimpleNamespace(isatty=lambda: True))
    assert rich_utils.FORCE_TERMINAL is None  # a terminal keeps what typer decided
    cli.plain_unless_terminal(types.SimpleNamespace(isatty=lambda: False))
    assert rich_utils.FORCE_TERMINAL is False
