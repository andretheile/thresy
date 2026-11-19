"""Codex adapter (not verified against the real CLI).

codex_docs_sample.jsonl is the sample stream from the official non-interactive
documentation; the other fixtures are synthetic, following the event types in
codex-rs/exec/src/exec_events.rs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from thresy.runner import HarnessConfig, Progress, Started
from thresy.runner.codex import CodexRunner, CodexStreamParser

from .runner_testkit import exit_info, fixture_lines, parse, request


def parser(tmp_path: Path, **kw) -> CodexStreamParser:
    return CodexStreamParser("codex", request(tmp_path, **kw), "gpt-test")


def test_docs_sample(tmp_path):
    events, result = parse(parser(tmp_path), fixture_lines("codex/codex_docs_sample.jsonl"))
    assert events == [
        Started("0199a213-81c0-7800-8aa1-bbab2a035a53"),
        Progress("tool", "shell: ls"),
        Progress("text", "Repo contains docs, sdk, and examples directories."),
    ]
    assert result.ok
    assert result.text == "Repo contains docs, sdk, and examples directories."
    assert result.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert result.usage["input_tokens"] == 24763
    assert result.usage["cache_read_tokens"] == 24448
    assert result.usage["output_tokens"] == 122
    assert result.cost_usd is None
    assert result.model == "gpt-test"


def test_all_item_types(tmp_path):
    events, result = parse(parser(tmp_path), fixture_lines("codex/codex_all_items.jsonl"))
    progress = [(e.kind, e.text) for e in events if isinstance(e, Progress)]
    assert progress == [
        ("tool", 'shell: coach sql "select 1"'),
        ("tool", "edit: memory/notes.md, memory/plan.md"),
        ("tool", "weather/forecast: tomorrow"),
        ("tool", "web search: marathon taper"),
        ("text", "Checking your data."),
        ("text", "Easy run today, 40 minutes."),
    ]
    assert all("TOOL OUTPUT" not in text for _, text in progress)
    assert result.ok and result.text == "Easy run today, 40 minutes."
    assert result.usage["reasoning_tokens"] == 20


def test_turn_failed_usage_limit(tmp_path):
    _, result = parse(
        parser(tmp_path), fixture_lines("codex/codex_turn_failed.jsonl"), exit_info(1)
    )
    assert not result.ok
    assert result.error_kind == "rate_limit"
    assert "usage limit" in result.error_detail
    assert result.session_id == "0199a213-81c0-7800-8aa1-000000000003"


def test_turn_failed_auth(tmp_path):
    lines = [
        '{"type":"thread.started","thread_id":"t-1"}',
        '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: '
        'Your access token could not be refreshed"}}',
    ]
    _, result = parse(parser(tmp_path), lines, exit_info(1))
    assert result.error_kind == "auth"


def test_error_event_without_turn_end(tmp_path):
    lines = ['{"type":"thread.started","thread_id":"t-2"}', '{"type":"error","message":"boom"}']
    _, result = parse(parser(tmp_path), lines, exit_info(1, stderr="fatal"))
    assert result.error_kind == "crash"
    assert "boom" in result.error_detail and "fatal" in result.error_detail


def test_timeout(tmp_path):
    lines = fixture_lines("codex/codex_docs_sample.jsonl")[:3]
    _, result = parse(parser(tmp_path), lines, exit_info(None, timed_out=True))
    assert result.error_kind == "timeout"


def test_command_line_new_session(tmp_path):
    inv = CodexRunner().build_invocation(request(tmp_path, prompt="Hi"), "codex", None)
    assert inv.argv == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]
    assert inv.stdin == b"Hi"


def test_command_line_resume_with_instructions(tmp_path):
    runner = CodexRunner(HarnessConfig(model_fast="gpt-fast", extra_args=("--ephemeral",)))
    req = request(
        tmp_path,
        prompt="Wie war mein Lauf?",
        session_id="t-9",
        model_tier="fast",
        system_append="Antworte kurz.",
    )
    inv = runner.build_invocation(req, "codex", runner.model_for("fast"))
    assert inv.argv == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path),
        "--model",
        "gpt-fast",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "resume",
        "t-9",
        "-",
    ]
    # Instructions travel with the prompt on stdin, never as an argument.
    assert inv.stdin == b"Antworte kurz.\n\n---\n\nWie war mein Lauf?"
    assert not any("Antworte" in arg or "Lauf" in arg for arg in inv.argv)
    assert inv.temp_dirs == ()


def test_models_default_to_cli_choice():
    assert CodexRunner().model_for("smart") is None
    assert not CodexRunner().capabilities.supports_system_append


def test_turn_completed_is_terminal(tmp_path):
    p = parser(tmp_path)
    for line in fixture_lines("codex/codex_docs_sample.jsonl")[:-1]:
        p.feed_line(line)
    assert not p.terminal_seen
    p.feed_line(fixture_lines("codex/codex_docs_sample.jsonl")[-1])
    assert p.terminal_seen


def test_healthcheck_login_status(tmp_path):
    script = tmp_path / "codex"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "codex-cli 0.130.0"; exit 0; fi\n'
        'echo "Logged in using ChatGPT (someone@example.org)"; exit 0\n'
    )
    script.chmod(0o755)
    health = asyncio.run(CodexRunner(HarnessConfig(binary=str(script))).healthcheck())
    assert health.ok and health.auth_ok and health.version == "0.130.0"
    assert not health.verified_adapter
    assert "example.org" not in health.detail
    assert any("not yet verified" in w for w in health.warnings)


def test_healthcheck_logged_out(tmp_path):
    script = tmp_path / "codex"
    script.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "codex-cli 0.130.0"; exit 0; fi\n'
        'echo "Not logged in"; exit 1\n'
    )
    script.chmod(0o755)
    health = asyncio.run(CodexRunner(HarnessConfig(binary=str(script))).healthcheck())
    assert health.auth_ok is False and not health.ok
