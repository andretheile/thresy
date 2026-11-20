"""`coach runner ...`: the typer plugin mounted into the project's CLI."""

from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

from thresy import cli as coach_cli
from thresy.runner import cli as runner_cli

invoke = CliRunner().invoke


def test_plugin_is_a_typer_app_mounted_as_runner():
    assert isinstance(runner_cli.app, typer.Typer)
    result = invoke(coach_cli.get_app(), ["runner", "--help"])
    assert result.exit_code == 0, result.output
    assert "check" in result.output and "run" in result.output


def test_check_fake_json(monkeypatch):
    monkeypatch.delenv("COACH_RUNNER", raising=False)
    result = invoke(coach_cli.get_app(), ["runner", "check", "--runner", "fake", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] and payload["runner"] == "fake"


def test_check_failure_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("COACH_CODEX_BIN", str(tmp_path / "missing-codex"))
    result = invoke(runner_cli.app, ["check", "--runner", "codex"])
    assert result.exit_code == 1
    assert "FAIL codex" in result.output


def test_check_bad_runner(monkeypatch):
    result = invoke(runner_cli.app, ["check", "--runner", "gemini", "--json"])
    assert result.exit_code == 2
    assert "unknown runner" in json.loads(result.output)["error"]


def test_run_fake_text_and_json(tmp_path):
    result = invoke(runner_cli.app, ["run", "--runner", "fake", "--cwd", str(tmp_path), "Hallo"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("[started]") and lines[1] == "[text] Hallo"
    assert lines[-1] == "Hallo"

    result = invoke(
        runner_cli.app, ["run", "--runner", "fake", "--cwd", str(tmp_path), "--json", "x"]
    )
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines()]
    assert [e["event"] for e in events] == ["started", "progress", "final"]


def test_run_reads_prompt_from_stdin(tmp_path):
    result = invoke(
        runner_cli.app,
        ["run", "--runner", "fake", "--cwd", str(tmp_path), "--tier", "fast", "-"],
        input="aus stdin",
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "aus stdin"


def test_run_passes_the_effort_level(monkeypatch, tmp_path):
    from thresy.runner import FakeRunner, cmdline

    fake = FakeRunner(reply="ok")
    monkeypatch.setattr(cmdline, "get_runner", lambda name, config: fake)
    base = ["run", "--runner", "fake", "--cwd", str(tmp_path)]
    result = invoke(runner_cli.app, [*base, "--effort", "xhigh", "x"])
    assert result.exit_code == 0, result.output
    result = invoke(runner_cli.app, [*base, "x"])
    assert [r.effort for r in fake.requests] == ["xhigh", None]
    bad = invoke(runner_cli.app, [*base, "--effort", "ultra", "x"])
    assert bad.exit_code == 2 and len(fake.requests) == 2


def test_config_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COACH_RUNNER", raising=False)
    monkeypatch.delenv("COACH_CLAUDE_MODEL_FAST", raising=False)
    (tmp_path / ".env").write_text("COACH_RUNNER=fake\nCOACH_CLAUDE_MODEL_FAST=from-dotenv\n")
    config = runner_cli._config()
    assert config.runner == "fake" and config.claude.model_fast == "from-dotenv"
    monkeypatch.setenv("COACH_RUNNER", "codex")  # the real environment wins
    assert runner_cli._config().runner == "codex"
