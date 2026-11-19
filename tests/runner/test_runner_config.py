"""Configuration, registry and the shared classification helpers."""

from __future__ import annotations

import pytest

from thresy.runner import HarnessConfig, RunnerConfig, get_runner
from thresy.runner.classify import classify_text, summarize_tool, truncate
from thresy.runner.claude import ClaudeRunner
from thresy.runner.codex import CodexRunner
from thresy.runner.cursor import CursorRunner


def test_from_env_defaults():
    config = RunnerConfig.from_env({})
    assert config.runner == "claude"
    assert config.claude == HarnessConfig()


def test_from_env_full():
    config = RunnerConfig.from_env(
        {
            "COACH_RUNNER": "Codex",
            "COACH_CODEX_BIN": "/usr/local/bin/codex",
            "COACH_CODEX_ARGS": "--ephemeral -c 'model_reasoning_effort=\"high\"'",
            "COACH_CODEX_PERMISSION_ARGS": "",
            "COACH_CODEX_MODEL_FAST": "gpt-fast",
            "COACH_CODEX_MODEL_SMART": "",
            "COACH_CODEX_ENV": "FOO=bar 'BAZ=with space'",
            "COACH_CODEX_PASS_ENV": "AWS_REGION, GOOGLE_*  EXTRA",
            "COACH_CODEX_EXIT_WAIT_S": "2.5",
            "COACH_CLAUDE_PERMISSION_ARGS": "--permission-mode auto",
        }
    )
    assert config.codex.pass_env == ("AWS_REGION", "GOOGLE_*", "EXTRA")
    assert config.codex.exit_wait_s == 2.5
    assert config.claude.exit_wait_s == 10.0 and config.claude.pass_env == ()
    assert config.runner == "codex"
    codex = config.codex
    assert codex.binary == "/usr/local/bin/codex"
    assert codex.extra_args == ("--ephemeral", "-c", 'model_reasoning_effort="high"')
    assert codex.permission_args == ()
    assert codex.model_fast == "gpt-fast" and codex.model_smart == ""
    assert codex.env == {"FOO": "bar", "BAZ": "with space"}
    assert config.claude.permission_args == ("--permission-mode", "auto")
    assert config.harness("cursor") == HarnessConfig()


@pytest.mark.parametrize(
    "environ",
    [
        {"COACH_RUNNER": "gemini"},
        {"COACH_CLAUDE_ENV": "NOEQUALS"},
        {"COACH_CLAUDE_ENV": "BAD-KEY=1"},
        {"COACH_CLAUDE_ARGS": "'unclosed"},
        {"COACH_CLAUDE_EXIT_WAIT_S": "soon"},
        {"COACH_CLAUDE_EXIT_WAIT_S": "-1"},
    ],
)
def test_from_env_errors(environ):
    with pytest.raises(ValueError):
        RunnerConfig.from_env(environ)


def test_get_runner():
    config = RunnerConfig(claude=HarnessConfig(model_smart="opus"))
    claude = get_runner("claude", config)
    assert isinstance(claude, ClaudeRunner) and claude.model_for("smart") == "opus"
    assert isinstance(get_runner("codex", config), CodexRunner)
    assert isinstance(get_runner("cursor", config), CursorRunner)
    assert isinstance(get_runner(None, RunnerConfig(runner="cursor")), CursorRunner)
    with pytest.raises(ValueError, match="unknown runner"):
        get_runner("gemini", config)


def test_permission_override():
    assert CodexRunner(HarnessConfig(permission_args=())).permission_args == ()
    assert CodexRunner().permission_args == ("--dangerously-bypass-approvals-and-sandbox",)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # Claude Code (docs: errors reference)
        ("You've hit your session limit · resets 3:45pm", "rate_limit"),
        ("You've hit your weekly limit · resets Mon 12:00am", "rate_limit"),
        (
            "You've hit your monthly spend limit · raise it at https://claude.ai/settings",
            "rate_limit",
        ),
        ("API Error: Repeated 529 Overloaded errors. The API is at capacity", "rate_limit"),
        (
            "API Error: Request rejected (429) · this may be a temporary capacity issue.",
            "rate_limit",
        ),
        ("API Error: Server is temporarily limiting requests (not your usage limit)", "rate_limit"),
        ("Credit balance is too low", "rate_limit"),
        ("Not logged in · Please run /login", "auth"),
        ("Login expired · Please run /login", "auth"),
        ("Failed to authenticate: OAuth session expired and could not be refreshed", "auth"),
        ("Failed to authenticate. API Error: 401 OAuth access token is invalid.", "auth"),
        ("Please run /login · API Error: 401 Invalid authentication credentials", "auth"),
        ("Invalid API key · Fix external API key", "auth"),
        # Codex / OpenAI style (synthetic)
        ("You've hit your usage limit. Try again in 3 days.", "rate_limit"),
        ("exceeded retry limit, last status: 429 Too Many Requests", "rate_limit"),
        ("unexpected status 401 Unauthorized", "auth"),
        # Cursor style (synthetic)
        ("Error: Not authenticated. Run agent login.", "auth"),
        # Not classifiable
        ("Error: model not found", None),
        ("line 401 of the log was strange", None),
        ("", None),
    ],
)
def test_classify_text(text, kind):
    assert classify_text(text) == kind


def test_summarize_tool():
    assert summarize_tool("Bash", {"command": "pytest -q", "description": "run tests"}) == (
        "Bash: pytest -q"
    )
    assert summarize_tool("Read", {"file_path": "/w/notes.md"}) == "Read: /w/notes.md"
    assert summarize_tool("mcp__weather__forecast", {"query": "x"}) == "weather/forecast: x"
    assert summarize_tool("TodoWrite", {"todos": [{"content": "a"}]}) == "TodoWrite"
    line = summarize_tool("Bash", {"command": "echo " + "y" * 200})
    assert len(line) <= len("Bash: ") + 60 and line.endswith("…")
    assert summarize_tool("Bash", {"command": "a\n  b"}) == "Bash: a b"


def test_truncate():
    assert truncate("short") == "short"
    assert truncate("x" * 100, 10) == "x" * 9 + "…"
