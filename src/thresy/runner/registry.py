"""get_runner: the single entry point other packages use."""

from __future__ import annotations

from .claude import ClaudeRunner
from .codex import CodexRunner
from .config import RUNNER_NAMES, RunnerConfig
from .core import AgentRunner
from .cursor import CursorRunner
from .fake import FakeRunner

_CLI_RUNNERS = {"claude": ClaudeRunner, "codex": CodexRunner, "cursor": CursorRunner}


def get_runner(name: str | None = None, config: RunnerConfig | None = None) -> AgentRunner:
    """Return the adapter for `name` (claude, codex, cursor or fake).

    `name` defaults to config.runner; `config` defaults to RunnerConfig.from_env().
    Raises ValueError for an unknown name or an invalid configuration.
    """
    config = config if config is not None else RunnerConfig.from_env()
    name = (name or config.runner).strip().lower()
    if name == "fake":
        return FakeRunner()
    cls = _CLI_RUNNERS.get(name)
    if cls is None:
        raise ValueError(f"unknown runner {name!r}; expected one of {', '.join(RUNNER_NAMES)}")
    return cls(config.harness(name), switch=config.user_switch)
