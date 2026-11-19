"""Cursor adapter (not verified against the real CLI).

cursor_docs_sequence.jsonl is the example sequence from Cursor's output-format
documentation (paths replaced by /workspace).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from runner_testkit import exit_info, fixture_lines, parse, request

from thresy.runner import HarnessConfig, Progress, Started
from thresy.runner.cursor import CursorRunner, CursorStreamParser, prepend_system

SESSION = "c6b62c6f-7ead-4fd6-9922-e952131177ff"


def parser(tmp_path: Path, **kw) -> CursorStreamParser:
    return CursorStreamParser("cursor", request(tmp_path, **kw), None)


def test_docs_sequence(tmp_path):
    events, result = parse(parser(tmp_path), fixture_lines("cursor/cursor_docs_sequence.jsonl"))
    assert events == [
        Started(SESSION),
        Progress("text", "I'll read the README.md file"),
        Progress("tool", "Read: README.md"),
        Progress("text", "Based on the README, I'll create a summary"),
        Progress("tool", "Write: summary.txt"),
        Progress("text", "Done! I've created the summary in summary.txt"),
    ]
    assert result.ok
    # The answer is the last segment, not the separator-less concatenation in "result".
    assert result.text == "Done! I've created the summary in summary.txt"
    assert result.session_id == SESSION
    assert result.model == "Claude 4 Sonnet"
    assert result.cost_usd is None and result.usage is None


def test_partial_output_duplicates_are_skipped(tmp_path):
    lines = [
        '{"type":"system","subtype":"init","session_id":"s-1","model":"m"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text",'
        '"text":"Hi"}]},"timestamp_ms":1,"model_call_id":"mc-1"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text",'
        '"text":"Hello"}]}}',
        '{"type":"tool_call","subtype":"started","call_id":"c1","tool_call":{"function":'
        '{"name":"run_terminal_cmd","arguments":"{\\"command\\": \\"ls -la\\"}"}}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"Hello",'
        '"session_id":"s-1"}',
    ]
    events, result = parse(parser(tmp_path), lines)
    assert events == [
        Started("s-1"),
        Progress("text", "Hello"),
        Progress("tool", "run_terminal_cmd: ls -la"),
    ]
    assert result.ok and result.text == "Hello"


def test_failure_on_stderr_is_classified(tmp_path):
    _, result = parse(
        parser(tmp_path), [], exit_info(1, stderr="Error: Not authenticated. Run agent login.")
    )
    assert result.error_kind == "auth"
    _, result = parse(parser(tmp_path), [], exit_info(1, stderr="rate limit exceeded, slow down"))
    assert result.error_kind == "rate_limit"
    _, result = parse(parser(tmp_path), [], exit_info(1, stderr="Error: something odd"))
    assert result.error_kind == "crash"


def test_error_result(tmp_path):
    lines = ['{"type":"result","subtype":"error","is_error":true,"result":"Usage limit reached"}']
    _, result = parse(parser(tmp_path), lines, exit_info(1))
    assert result.error_kind == "rate_limit"


def test_command_line(tmp_path):
    runner = CursorRunner(HarnessConfig(model_smart="sonnet-4"))
    req = request(
        tmp_path, prompt="-starts with a dash", session_id="s-7", system_append="Be brief."
    )
    inv = runner.build_invocation(req, "agent", runner.model_for("smart"))
    assert inv.argv == [
        "agent",
        "-p",
        "--output-format",
        "stream-json",
        "--workspace",
        str(tmp_path),
        "--model",
        "sonnet-4",
        "--resume=s-7",
        "--force",
        "--trust",
        "--",
        "Be brief.\n\n---\n\n-starts with a dash",
    ]
    assert inv.stdin is None
    assert not runner.capabilities.supports_system_append


def test_prepend_system():
    assert prepend_system("hi", None) == "hi"
    assert prepend_system("hi", "  ") == "hi"
    assert prepend_system("hi", "rules") == "rules\n\n---\n\nhi"


def test_binary_fallback_names(tmp_path):
    (tmp_path / "cursor-agent").write_text("#!/bin/sh\n")
    (tmp_path / "cursor-agent").chmod(0o755)
    assert CursorRunner().resolve_binary({"PATH": str(tmp_path)}) == str(tmp_path / "cursor-agent")


def test_healthcheck_status_json(tmp_path):
    script = tmp_path / "agent"
    script.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2026.11.01-abc"; exit 0; fi\n'
        """echo '{"isAuthenticated": true, "email": "someone@example.org"}'\n"""
    )
    script.chmod(0o755)
    health = asyncio.run(CursorRunner(HarnessConfig(binary=str(script))).healthcheck())
    assert health.auth_ok is True and health.ok
    assert "example.org" not in health.detail
