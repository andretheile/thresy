"""Claude Code adapter: stream mapping, classification, command line.

claude_new_session / claude_resume_tool_call / claude_auth_invalid_token are
real Claude Code 2.1.327 output (anonymised). The other claude_* fixtures are
synthetic, built from the documented message schema and error texts.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from thresy.runner import HarnessConfig, Progress, Started
from thresy.runner.claude import ClaudeRunner, ClaudeStreamParser

from .runner_testkit import (
    exit_info,
    fixture_lines,
    group_alive,
    parse,
    read_json,
    request,
    run_collect,
    spec_env,
    wait_until,
    write_fake_cli,
)

SESSION = "11111111-1111-4111-8111-000000000001"


def parser(tmp_path: Path, **kw) -> ClaudeStreamParser:
    return ClaudeStreamParser("claude", request(tmp_path, **kw), "haiku")


def test_new_session_real_capture(tmp_path):
    events, result = parse(parser(tmp_path), fixture_lines("claude/claude_new_session.jsonl"))
    assert events == [Started(SESSION), Progress("text", "OK")]
    assert result.ok
    assert result.text == "OK"
    assert result.session_id == SESSION
    assert result.model == "claude-haiku-5-5"
    assert result.error_kind is None and result.error_detail is None
    assert result.cost_usd == pytest.approx(0.0024748)
    assert result.usage["input_tokens"] == 2
    assert result.usage["output_tokens"] == 4
    assert result.usage["cache_read_tokens"] == 9160
    assert result.usage["cache_write_tokens"] == 11905
    assert "iterations" not in result.usage["raw"]


def test_resume_with_tool_call_real_capture(tmp_path):
    events, result = parse(
        parser(tmp_path, session_id=SESSION), fixture_lines("claude/claude_resume_tool_call.jsonl")
    )
    assert events == [Started(SESSION), Progress("tool", "Bash: pwd"), Progress("text", "OK")]
    assert result.ok and result.text == "OK"
    assert result.session_id == SESSION
    # total_cost_usd (0.00513247) includes the first run; only this run's share counts.
    assert result.cost_usd == pytest.approx(0.00265767)
    # Tool output (the working directory) never appears in progress.
    assert all("/workspace" not in e.text for e in events if isinstance(e, Progress))


def test_invalid_token_real_capture_is_auth(tmp_path):
    events, result = parse(
        parser(tmp_path),
        fixture_lines("claude/claude_auth_invalid_token.jsonl"),
        exit_info(1),
    )
    assert events == [Started("11111111-1111-4111-8111-000000000002")]
    assert not result.ok
    assert result.error_kind == "auth"
    assert "401" in result.error_detail
    assert "error=authentication_failed" in result.error_detail
    assert result.text == ""  # the API error message is not agent output
    assert result.cost_usd == 0


@pytest.mark.parametrize(
    ("fixture", "kind", "needle"),
    [
        ("claude_usage_limit.jsonl", "rate_limit", "session limit"),
        ("claude_overloaded.jsonl", "rate_limit", "529"),
        ("claude_max_turns.jsonl", "other", "maximum number of turns"),
        ("claude_not_logged_in_text.jsonl", "auth", "Not logged in"),
        ("claude_startup_failure.jsonl", "other", "not valid JSON"),
    ],
)
def test_documented_failures(tmp_path, fixture, kind, needle):
    _, result = parse(parser(tmp_path), fixture_lines(f"claude/{fixture}"), exit_info(1))
    assert not result.ok
    assert result.error_kind == kind
    assert needle in result.error_detail


def test_max_turns_progress_hides_tool_output_and_subagents(tmp_path):
    events, result = parse(parser(tmp_path), fixture_lines("claude/claude_max_turns.jsonl"))
    texts = [e.text for e in events if isinstance(e, Progress)]
    assert texts == [
        "Let me look at the training log.",
        "Read: /workspace/memory/training-log.md",
    ]
    assert result.text == "Let me look at the training log."
    assert result.cost_usd == pytest.approx(0.02)


def test_usage_limit_detail_carries_reset_time(tmp_path):
    _, result = parse(parser(tmp_path), fixture_lines("claude/claude_usage_limit.jsonl"))
    assert "resets_at=1800000000" in result.error_detail


def test_garbage_lines_are_ignored(tmp_path):
    lines = fixture_lines("claude/claude_new_session.jsonl")
    noisy = ["Warning: something on stdout", "", "[1, 2]", *lines[:2], "{not json", *lines[2:]]
    events, result = parse(parser(tmp_path), noisy)
    assert result.ok and result.text == "OK"
    assert len(events) == 2


def test_no_result_nonzero_exit_is_crash(tmp_path):
    lines = fixture_lines("claude/claude_new_session.jsonl")[:1]
    _, result = parse(parser(tmp_path), lines, exit_info(2, stderr="Error: boom\n"))
    assert result.error_kind == "crash"
    assert "exit code 2" in result.error_detail and "boom" in result.error_detail
    assert result.session_id == SESSION


def test_no_result_with_auth_text_on_stderr(tmp_path):
    _, result = parse(
        parser(tmp_path), [], exit_info(1, stderr="Not logged in · Please run /login")
    )
    assert result.error_kind == "auth"


def test_timeout_without_result(tmp_path):
    lines = fixture_lines("claude/claude_new_session.jsonl")[:2]
    _, result = parse(parser(tmp_path), lines, exit_info(None, timed_out=True))
    assert result.error_kind == "timeout"
    assert result.text == "OK"  # partial text survives


def test_result_then_timeout_keeps_the_answer(tmp_path):
    lines = fixture_lines("claude/claude_new_session.jsonl")
    _, result = parse(parser(tmp_path), lines, exit_info(-2, timed_out=True))
    assert result.ok and result.text == "OK"


def test_cancelled(tmp_path):
    p = parser(tmp_path)
    p.cancel_reason = "user asked to stop"
    _, result = parse(
        p, fixture_lines("claude/claude_new_session.jsonl"), exit_info(-2, cancelled=True)
    )
    assert result.error_kind == "cancelled"
    assert result.error_detail == "user asked to stop"


# -- command line -----------------------------------------------------------------


def test_command_line_new_session(tmp_path):
    runner = ClaudeRunner()
    inv = runner.build_invocation(request(tmp_path, prompt="Hallo"), "claude", "sonnet")
    assert inv.argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "sonnet",
        "--permission-mode",
        "bypassPermissions",
        "--permission-prompts",
        "none",
    ]
    assert inv.stdin == b"Hallo"


def test_command_line_resume_with_system_append_and_config(tmp_path):
    config = HarnessConfig(
        extra_args=("--max-turns", "30"), permission_args=("--permission-mode", "auto")
    )
    runner = ClaudeRunner(config)
    req = request(tmp_path, session_id=SESSION, system_append="Speak German.", model_tier="fast")
    inv = runner.build_invocation(req, "/opt/claude", runner.model_for("fast"))
    try:
        file_arg = inv.argv[inv.argv.index("--append-system-prompt-file") + 1]
        assert inv.argv == [
            "/opt/claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "haiku",
            "--resume",
            SESSION,
            "--append-system-prompt-file",
            file_arg,
            "--permission-mode",
            "auto",
            "--max-turns",
            "30",
        ]
        # The instructions are in a file only this user can read, not in argv.
        path = Path(file_arg)
        assert path.read_text() == "Speak German."
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert inv.temp_dirs == (path.parent,)
        assert "Speak German." not in " ".join(inv.argv)
        assert "--bare" not in inv.argv
    finally:
        inv.cleanup()
    assert not path.parent.exists()


def test_model_tiers():
    assert ClaudeRunner().model_for("fast") == "haiku"
    assert ClaudeRunner().model_for("smart") == "sonnet"
    assert ClaudeRunner(HarnessConfig(model_smart="opus")).model_for("smart") == "opus"
    assert ClaudeRunner(HarnessConfig(model_smart="")).model_for("smart") is None


def test_bare_is_refused():
    with pytest.raises(ValueError, match="--bare"):
        ClaudeRunner(HarnessConfig(extra_args=("--bare",)))
    with pytest.raises(ValueError, match="CLAUDE_CODE_SIMPLE"):
        ClaudeRunner(HarnessConfig(env={"CLAUDE_CODE_SIMPLE": "1"}))


# -- end to end through a fake binary -------------------------------------------


def test_end_to_end_with_fake_binary(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    record = tmp_path / "record.json"
    fixture = Path(__file__).parent / "fixtures/claude/claude_resume_tool_call.jsonl"
    # The service's own secrets must not reach the agent; the harness's auth must.
    monkeypatch.setenv("COACH_TELEGRAM_TOKEN", "secret-telegram")
    monkeypatch.setenv("COACH_DATABASE_URL", "postgresql://app:secret@db/coach")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    env = spec_env(
        record=str(record),
        env_keys=[
            "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
            "FROM_CONFIG",
            "FROM_REQUEST",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_SIMPLE",
            "COACH_TELEGRAM_TOKEN",
            "COACH_DATABASE_URL",
        ],
        steps=[{"out_file": str(fixture)}],
    )
    config = HarnessConfig(binary=str(write_fake_cli(bin_dir)), env={**env, "FROM_CONFIG": "c"})
    req = request(
        work,
        prompt="Wie war mein Lauf?",
        session_id=SESSION,
        env={"FROM_REQUEST": "r", "COACH_DATABASE_URL": "postgresql://agent@db/coach"},
        system_append="Antworte auf Deutsch.",
    )
    events, result = run_collect(ClaudeRunner(config).run(req))
    assert result.ok and result.text == "OK" and result.runner == "claude"
    assert result.duration_s > 0
    assert [type(e).__name__ for e in events] == ["Started", "Progress", "Progress", "Final"]
    seen = read_json(record)
    assert seen["stdin"] == "Wie war mein Lauf?"
    assert Path(seen["cwd"]).resolve() == work.resolve()
    assert seen["argv"][:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert "--resume" in seen["argv"] and SESSION in seen["argv"]
    assert seen["env"] == {
        "CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "1",
        "FROM_CONFIG": "c",
        "FROM_REQUEST": "r",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test",
        "CLAUDE_CODE_SIMPLE": None,
        "COACH_TELEGRAM_TOKEN": None,
        "COACH_DATABASE_URL": "postgresql://agent@db/coach",  # only the one req.env hands over
    }
    assert seen["pgid"] == seen["pid"]  # its own process group
    # system_append arrived through a private file that is gone after the run.
    (file_arg, info), *_ = seen["arg_files"].items()
    assert seen["argv"][seen["argv"].index("--append-system-prompt-file") + 1] == file_arg
    assert info == {"text": "Antworte auf Deutsch.", "mode": "0o600", "dir_mode": "0o700"}
    assert not Path(file_arg).exists()
    assert "Antworte" not in " ".join(seen["argv"])


def test_lingering_cli_does_not_hold_back_the_answer(tmp_path):
    record = tmp_path / "record.json"
    fixture = Path(__file__).parent / "fixtures/claude/claude_new_session.jsonl"
    env = spec_env(record=str(record), steps=[{"out_file": str(fixture)}, {"sleep": 600}])
    config = HarnessConfig(binary=str(write_fake_cli(tmp_path)), env=env, exit_wait_s=0.5)
    start = time.monotonic()
    _, result = run_collect(
        ClaudeRunner(config, kill_grace_s=0.3).run(request(tmp_path, timeout_s=120))
    )
    assert time.monotonic() - start < 10
    assert result.ok and result.text == "OK"
    pgid = read_json(record)["pgid"]
    assert wait_until(lambda: not group_alive(pgid))


def test_unspawnable_command_line_ends_with_final(tmp_path):
    config = HarnessConfig(binary=str(write_fake_cli(tmp_path)), extra_args=("bad\x00arg",))
    events, result = run_collect(ClaudeRunner(config).run(request(tmp_path, system_append="x")))
    assert len(events) == 1
    assert result.error_kind == "other" and "could not start" in result.error_detail


def test_healthcheck_parses_auth_status(tmp_path):
    script = tmp_path / "claude"
    status = {
        "loggedIn": True,
        "authMethod": "oauth_token",
        "apiProvider": "firstParty",
        "email": "someone@example.org",
        "orgId": "org-123",
    }
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.327 (Claude Code)"; exit 0; fi\n'
        f"echo '{json.dumps(status)}'\n"
    )
    script.chmod(0o755)
    health = asyncio.run(ClaudeRunner(HarnessConfig(binary=str(script))).healthcheck())
    assert health.ok and health.auth_ok and health.binary_found
    assert health.version == "2.1.327"
    assert health.verified_adapter
    assert "oauth_token" in health.detail
    assert "example.org" not in health.detail and "org-123" not in health.detail


def test_healthcheck_not_logged_in(tmp_path):
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.327 (Claude Code)"; exit 0; fi\n'
        """echo '{"loggedIn": false, "authMethod": "none"}'; exit 1\n"""
    )
    script.chmod(0o755)
    health = asyncio.run(ClaudeRunner(HarnessConfig(binary=str(script))).healthcheck())
    assert health.auth_ok is False and not health.ok
    assert "setup-token" in health.detail


def test_healthcheck_missing_binary(tmp_path):
    health = asyncio.run(ClaudeRunner(HarnessConfig(binary=str(tmp_path / "nope"))).healthcheck())
    assert not health.binary_found and not health.ok
