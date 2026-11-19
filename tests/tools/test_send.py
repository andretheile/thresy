"""coach send: outbox rows from the agent's role, path rules, clean failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from thresy.cli import get_app
from thresy.config import get_settings
from thresy.tools import send

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def coach(args: list[str], env: dict[str, str], input: str | None = None) -> Any:
    """Run `coach …` with exactly these COACH_* settings (others unset)."""
    clean = {k: None for k in os.environ if k.startswith("COACH_")}
    clean.update(env)
    get_settings.cache_clear()
    try:
        return CliRunner().invoke(get_app(), args, env=clean, input=input)
    finally:
        get_settings.cache_clear()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "out").mkdir(parents=True)
    return ws


@pytest.fixture
def agent_env(test_db, workspace: Path, monkeypatch) -> dict[str, str]:
    monkeypatch.chdir(workspace)  # the agent's working directory; also: no .env here
    return {
        "COACH_DATABASE_URL": test_db.agent_url,
        "COACH_WORKSPACE_DIR": str(workspace),
        "COACH_RUN_ID": "41",
    }


# Path rules (no database) ---------------------------------------------------------------------


def test_check_file_accepts_files_under_out(workspace: Path) -> None:
    plot = workspace / "out" / "plot.png"
    plot.write_bytes(PNG)
    assert send.check_file(plot, workspace, kind="photo") == plot.resolve()
    assert send.check_file("out/plot.png", workspace, kind="photo", cwd=workspace) == plot.resolve()
    nested = workspace / "out" / "a" / "b.csv"
    nested.parent.mkdir()
    nested.write_text("x")
    assert send.check_file(nested, workspace, kind="document") == nested.resolve()


@pytest.mark.parametrize(
    "make",
    [
        lambda ws, tmp: ws / "notes.md",  # in the workspace but not in out/
        lambda ws, tmp: tmp / "elsewhere.png",  # outside the workspace
        lambda ws, tmp: ws / "out" / ".." / "notes.md",  # dot-dot escape
    ],
)
def test_check_file_refuses_paths_outside_out(workspace: Path, tmp_path: Path, make) -> None:
    (workspace / "notes.md").write_text("secret")
    (tmp_path / "elsewhere.png").write_bytes(PNG)
    with pytest.raises(send.SendError, match="must be under"):
        send.check_file(make(workspace, tmp_path), workspace, kind="photo")


def test_check_file_resolves_symlinks(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    link = workspace / "out" / "innocent.png"
    link.symlink_to(secret)
    with pytest.raises(send.SendError, match="must be under"):
        send.check_file(link, workspace, kind="photo")
    # A symlinked out/ directory pointing elsewhere does not widen the rule either.
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.png").write_bytes(PNG)
    escape = workspace / "out" / "dir"
    escape.symlink_to(other, target_is_directory=True)
    with pytest.raises(send.SendError, match="must be under"):
        send.check_file(escape / "x.png", workspace, kind="photo")


def test_check_file_other_errors(workspace: Path) -> None:
    with pytest.raises(send.SendError, match="not found"):
        send.check_file(workspace / "out" / "missing.png", workspace, kind="photo")
    (workspace / "out" / "empty.png").write_bytes(b"")
    with pytest.raises(send.SendError, match="empty"):
        send.check_file(workspace / "out" / "empty.png", workspace, kind="photo")
    with pytest.raises(send.SendError, match="not a regular file"):
        send.check_file(workspace / "out", workspace, kind="document")
    big = workspace / "out" / "big.png"
    with big.open("wb") as fh:
        fh.truncate(11 * 1024 * 1024)
    with pytest.raises(send.SendError, match="limit for a photo"):
        send.check_file(big, workspace, kind="photo")


def test_options_and_run_id_validation() -> None:
    assert send.check_options([" Yes ", "No"]) == ["Yes", "No"]
    for bad in ([], [""], ["x"] * 11, ["a", "a"], ["y" * 65]):
        with pytest.raises(send.SendError):
            send.check_options(bad)
    assert send.run_id_from_env({}) is None
    assert send.run_id_from_env({"COACH_RUN_ID": "7"}) == 7
    with pytest.raises(send.SendError):
        send.run_id_from_env({"COACH_RUN_ID": "seven"})


# The command, with the agent's role -----------------------------------------------------------


@pytest.mark.db
def test_send_text_photo_document_choice_as_the_agent(conn, agent_env, workspace) -> None:
    (workspace / "out" / "plot.png").write_bytes(PNG)
    (workspace / "out" / "route.gpx").write_text("<gpx/>")
    results = [
        coach(["send", "text", "Plot **below**"], agent_env),
        coach(["send", "photo", "out/plot.png", "--caption", "HR zones"], agent_env),
        coach(["send", "document", str(workspace / "out" / "route.gpx")], agent_env),
        coach(["send", "choice", "Tomorrow?", "-o", "Easy", "-o", "Rest", "--json"], agent_env),
        coach(["send", "text", "-"], agent_env, input="from stdin\n"),
    ]
    for result in results:
        assert result.exit_code == 0, result.output
    assert "queued text #1" in results[0].output
    assert json.loads(results[3].output) == {"id": 4, "kind": "choice", "run_id": 41, "file": None}
    rows = conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
    assert [(r["kind"], r["text"], r["run_id"], r["chat_id"]) for r in rows] == [
        ("text", "Plot **below**", 41, None),
        ("photo", "HR zones", 41, None),
        ("document", None, 41, None),
        ("choice", "Tomorrow?", 41, None),
        ("text", "from stdin\n", 41, None),
    ]
    assert rows[1]["file_path"] == str((workspace / "out" / "plot.png").resolve())
    assert rows[3]["options"] == ["Easy", "Rest"]
    assert all(r["sent_at"] is None for r in rows)


@pytest.mark.db
def test_send_refuses_files_outside_out(conn, agent_env, workspace) -> None:
    (workspace / "AGENTS.md").write_text("x")
    result = coach(["send", "document", "AGENTS.md"], agent_env)
    assert result.exit_code == 1
    assert "must be under" in result.output
    result = coach(["send", "photo", "/etc/hosts", "--json"], agent_env)
    assert result.exit_code == 1 and "must be under" in json.loads(result.output)["error"]
    assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 0


@pytest.mark.db
def test_send_fails_cleanly_with_a_role_that_may_not_insert(conn, agent_env, test_db) -> None:
    env = {**agent_env, "COACH_DATABASE_URL": test_db.ro_url}
    result = coach(["send", "text", "hello"], env)
    assert result.exit_code == 1
    assert "may not queue messages" in result.output
    assert "Traceback" not in result.output


@pytest.mark.db
def test_send_without_database_url_or_with_bad_input(agent_env) -> None:
    env = {k: v for k, v in agent_env.items() if k != "COACH_DATABASE_URL"}
    result = coach(["send", "text", "hello"], env)
    assert result.exit_code == 1 and "COACH_DATABASE_URL" in result.output
    result = coach(["send", "text", "   "], agent_env)
    assert result.exit_code == 1 and "must not be empty" in result.output
    result = coach(["send", "choice", "Q?", "-o", "same", "-o", "same"], agent_env)
    assert result.exit_code == 1 and "different" in result.output


def test_help_is_written_for_the_agent() -> None:
    result = CliRunner().invoke(get_app(), ["send", "--help"])
    assert result.exit_code == 0
    assert "NO_REPLY" in result.output and "out/" in result.output
    result = CliRunner().invoke(get_app(), ["sql", "--help"])
    assert "wake_date" in result.output and "--limit" in result.output
