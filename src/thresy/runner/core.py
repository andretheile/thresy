"""Public types of the runner package: requests, events, results and the protocol.

Nothing in here knows about a specific harness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .handle import RunHandle

ModelTier = Literal["fast", "smart"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
ErrorKind = Literal["rate_limit", "auth", "timeout", "crash", "cancelled", "other"]
ProgressKind = Literal["text", "tool"]

EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")
"""Reasoning-effort levels, lowest first."""

ERROR_KINDS: tuple[ErrorKind, ...] = (
    "rate_limit",
    "auth",
    "timeout",
    "crash",
    "cancelled",
    "other",
)


@dataclass(frozen=True)
class RunRequest:
    """One agent run: a prompt, a working directory and how to run it."""

    prompt: str
    cwd: Path
    session_id: str | None = None
    """Harness-native session id to resume; None starts a new session."""
    model_tier: ModelTier = "smart"
    timeout_s: float = 900
    env: Mapping[str, str] = field(default_factory=dict)
    """Added to the child environment (on top of the parent environment)."""
    system_append: str | None = None
    """Extra system instructions; prepended to the prompt if the harness has no
    such option (see RunnerCapabilities.supports_system_append)."""
    effort: Effort | None = None
    """Reasoning-effort hint; None: the harness's (or model's) default. Each adapter
    maps it to its CLI's own setting; one without such a setting ignores it (see
    RunnerCapabilities.supports_effort). A level the model does not support never
    fails the run (docs/runners.md, "Reasoning effort")."""


def parse_effort(value: str | None) -> Effort | None:
    """An effort level from configuration or a command line: one of EFFORTS (any case);
    None or empty mean the harness's default. Raises ValueError otherwise."""
    if value is None or not value.strip():
        return None
    level = value.strip().lower()
    for known in EFFORTS:
        if level == known:
            return known
    raise ValueError(f"unknown effort level {value!r}; expected one of {', '.join(EFFORTS)}")


@dataclass(frozen=True)
class RunResult:
    ok: bool
    text: str
    """The agent's final answer. On failure: whatever assistant text arrived
    before the failure (often empty)."""
    session_id: str | None
    """Harness-native session id of this run (pass it back to resume)."""
    error_kind: ErrorKind | None
    error_detail: str | None
    cost_usd: float | None
    """Estimated cost of this run as reported by the harness, if it reports one."""
    usage: dict | None
    """Token usage with normalised keys (input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, reasoning_tokens; absent when unknown)
    plus the harness's own figures under "raw"."""
    duration_s: float
    runner: str
    model: str | None


@dataclass(frozen=True)
class Started:
    """The harness reported its session id. Emitted at most once per run."""

    session_id: str | None


@dataclass(frozen=True)
class Progress:
    """Something happened that a user may want to see while waiting.

    kind "text": an assistant text block (the final answer is also emitted as
    Progress before the Final, so do not send both). kind "tool": one short
    human-readable line per tool call, for example "Bash: pytest -q".
    """

    kind: ProgressKind
    text: str


@dataclass(frozen=True)
class Final:
    """Always the last event of a run, emitted exactly once."""

    result: RunResult


RunEvent = Started | Progress | Final


@dataclass(frozen=True)
class RunnerCapabilities:
    supports_resume: bool
    supports_streaming: bool
    supports_system_append: bool
    supports_effort: bool = False
    """Whether RunRequest.effort reaches the harness; False: the hint is ignored."""


@dataclass(frozen=True)
class Health:
    """Result of AgentRunner.healthcheck(). Never spends model usage."""

    runner: str
    binary_found: bool
    binary_path: str | None
    version: str | None
    auth_ok: bool | None
    """True: credentials look usable. False: they clearly are not. None: the
    CLI offers no way to tell without a model request."""
    detail: str
    """Human-readable summary, safe to show in a chat (no account details)."""
    verified_adapter: bool
    """False for adapters that have not been run against the real CLI yet."""
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.binary_found and self.auth_ok is not False


@runtime_checkable
class AgentRunner(Protocol):
    name: str
    capabilities: RunnerCapabilities

    def run(self, req: RunRequest) -> RunHandle:
        """Start a run. The returned RunHandle (an AsyncIterator[RunEvent] with
        cancel(), result() and aclose()) yields Started at most once, any number
        of Progress events, and ends with exactly one Final. It never raises for
        agent-side failures."""
        ...

    async def healthcheck(self) -> Health: ...
