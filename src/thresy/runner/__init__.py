"""One interface over coding-agent CLIs (Claude Code, Codex, Cursor) plus a fake.

This package is the only place in the code base that knows about a specific
harness. Standard library only. See docs/runners.md.

    from thresy.runner import RunRequest, get_runner

    runner = get_runner()                      # COACH_RUNNER, default "claude"
    async with runner.run(RunRequest(prompt="Hi", cwd=workspace)) as run:
        async for event in run:
            ...                                # Started, Progress..., Final
"""

from .config import RUNNER_NAMES, HarnessConfig, RunnerConfig
from .core import (
    EFFORTS,
    ERROR_KINDS,
    AgentRunner,
    Effort,
    ErrorKind,
    Final,
    Health,
    ModelTier,
    Progress,
    RunEvent,
    RunnerCapabilities,
    RunRequest,
    RunResult,
    Started,
    parse_effort,
)
from .fake import Delay, Fail, FakeRunner
from .handle import RunHandle
from .registry import get_runner

__all__ = [
    "EFFORTS",
    "ERROR_KINDS",
    "RUNNER_NAMES",
    "AgentRunner",
    "Delay",
    "Effort",
    "ErrorKind",
    "Fail",
    "FakeRunner",
    "Final",
    "HarnessConfig",
    "Health",
    "ModelTier",
    "Progress",
    "RunEvent",
    "RunHandle",
    "RunRequest",
    "RunResult",
    "RunnerCapabilities",
    "RunnerConfig",
    "Started",
    "get_runner",
    "parse_effort",
]
