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


def test_plugins_are_discovered_and_missing_ones_skipped(fake_plugins) -> None:
    root = _fresh_root()
    mounted = cli.register_plugins(root)
    assert mounted == ["sync", "tools"]  # importer, analysis, bridge, runner do not exist here

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
