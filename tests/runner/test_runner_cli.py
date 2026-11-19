"""`python -m thresy.runner` with the fake runner."""

from __future__ import annotations

import json

from thresy.runner.cli import main


def test_run_prints_events(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("COACH_RUNNER", raising=False)
    code = main(["run", "--runner", "fake", "--cwd", str(tmp_path), "Hallo Coach"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("[started] session fake-")
    assert out[1] == "[text] Hallo Coach"
    assert out[2].startswith("[final] ok; runner fake")
    assert out[3] == "Hallo Coach"


def test_run_json(tmp_path, capsys):
    code = main(["run", "--runner", "fake", "--cwd", str(tmp_path), "--json", "x"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert [e["event"] for e in events] == ["started", "progress", "final"]
    assert events[-1]["result"]["ok"] is True


def test_check_json(capsys):
    code = main(["check", "--runner", "fake", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] and payload["runner"] == "fake"


def test_check_missing_cli(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("COACH_CODEX_BIN", str(tmp_path / "missing-codex"))
    code = main(["check", "--runner", "codex"])
    assert code == 1
    assert "FAIL codex" in capsys.readouterr().out


def test_bad_config_exit_code(capsys, monkeypatch):
    monkeypatch.setenv("COACH_RUNNER", "nonsense")
    assert main(["check"]) == 2
    assert "COACH_RUNNER" in capsys.readouterr().err
