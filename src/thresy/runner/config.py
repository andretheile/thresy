"""Configuration of the runner package: plain dataclasses plus a COACH_* reader.

Per harness NAME (CLAUDE, CODEX, CURSOR) the following variables are read:

    COACH_<NAME>_BIN               path or name of the CLI binary
    COACH_<NAME>_ARGS              extra arguments, shell-quoted ("--max-turns 40")
    COACH_<NAME>_PERMISSION_ARGS   replaces the adapter's unattended-permission
                                   arguments; set it to an empty string for none
    COACH_<NAME>_MODEL_FAST        model for the "fast" tier
    COACH_<NAME>_MODEL_SMART       model for the "smart" tier
                                   (unset: adapter default; empty: let the CLI choose)
    COACH_<NAME>_ENV               extra environment, shell-quoted KEY=VALUE pairs
    COACH_<NAME>_PASS_ENV          names (or prefixes ending in *) of parent
                                   variables the CLI may inherit beyond the
                                   built-in allow-list, separated by spaces or commas
    COACH_<NAME>_EXIT_WAIT_S       seconds the CLI gets to exit after its final
                                   result before it is stopped (default 10)

and COACH_RUNNER selects the default runner (claude, codex, cursor or fake).

For all harnesses (see thresy.runner.isolation):

    COACH_AGENT_USER               start every CLI process as this Unix user, through
                                   the setuid helper (unset or empty: off)
    COACH_AGENT_USER_HELPER        path of that helper
                                   (default /usr/local/libexec/thresy/run-as-agent)
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field

from .core import ModelTier
from .isolation import UserSwitch, switch_from_env

RUNNER_NAMES: tuple[str, ...] = ("claude", "codex", "cursor", "fake")
DEFAULT_RUNNER = "claude"
DEFAULT_EXIT_WAIT_S = 10.0


@dataclass(frozen=True)
class HarnessConfig:
    """Settings for one harness adapter."""

    binary: str | None = None
    """Path or command name. None: the adapter's default name, looked up on PATH."""
    extra_args: tuple[str, ...] = ()
    """Appended to every run's command line."""
    permission_args: tuple[str, ...] | None = None
    """Unattended-permission arguments. None: the adapter's default (the
    container is the sandbox). An empty tuple passes none."""
    model_fast: str | None = None
    model_smart: str | None = None
    """None: the adapter's default model for the tier. "": pass no model
    argument and let the CLI use its own default."""
    env: Mapping[str, str] = field(default_factory=dict)
    """Added to the child environment of every run (RunRequest.env wins)."""
    pass_env: tuple[str, ...] = ()
    """Parent variables the CLI may inherit beyond the built-in allow-list:
    exact names, or prefixes ending in "*" (see thresy.runner.environment)."""
    exit_wait_s: float = DEFAULT_EXIT_WAIT_S
    """After the harness reported its final result, how long the CLI gets to
    exit on its own before its process group is stopped."""

    def model_for(self, tier: ModelTier, defaults: Mapping[str, str | None]) -> str | None:
        configured = self.model_fast if tier == "fast" else self.model_smart
        if configured is None:
            return defaults.get(tier) or None
        return configured or None


@dataclass(frozen=True)
class RunnerConfig:
    """Settings for all adapters plus the name of the default runner."""

    runner: str = DEFAULT_RUNNER
    claude: HarnessConfig = field(default_factory=HarnessConfig)
    codex: HarnessConfig = field(default_factory=HarnessConfig)
    cursor: HarnessConfig = field(default_factory=HarnessConfig)
    user_switch: UserSwitch | None = None
    """Start the CLIs as another Unix user (COACH_AGENT_USER); None: as this process's user."""

    def harness(self, name: str) -> HarnessConfig:
        if name == "claude":
            return self.claude
        if name == "codex":
            return self.codex
        if name == "cursor":
            return self.cursor
        return HarnessConfig()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RunnerConfig:
        """Build the configuration from COACH_* variables.

        `environ` defaults to os.environ; pass another mapping (for example
        values merged from a .env file) to read from there instead.
        """
        env = os.environ if environ is None else environ
        runner = (env.get("COACH_RUNNER") or DEFAULT_RUNNER).strip().lower()
        if runner not in RUNNER_NAMES:
            raise ValueError(f"COACH_RUNNER={runner!r} is not one of {', '.join(RUNNER_NAMES)}")
        return cls(
            runner=runner,
            claude=_harness_from_env(env, "CLAUDE"),
            codex=_harness_from_env(env, "CODEX"),
            cursor=_harness_from_env(env, "CURSOR"),
            user_switch=switch_from_env(env),
        )


def _harness_from_env(env: Mapping[str, str], name: str) -> HarnessConfig:
    prefix = f"COACH_{name}_"

    def get(key: str) -> str | None:
        return env.get(prefix + key)

    permission = get("PERMISSION_ARGS")
    return HarnessConfig(
        binary=(get("BIN") or "").strip() or None,
        extra_args=tuple(_split(get("ARGS"), prefix + "ARGS")),
        permission_args=(
            None if permission is None else tuple(_split(permission, prefix + "PERMISSION_ARGS"))
        ),
        model_fast=_model(get("MODEL_FAST")),
        model_smart=_model(get("MODEL_SMART")),
        env=_parse_env_pairs(get("ENV"), prefix + "ENV"),
        pass_env=tuple(_split((get("PASS_ENV") or "").replace(",", " "), prefix + "PASS_ENV")),
        exit_wait_s=_seconds(get("EXIT_WAIT_S"), prefix + "EXIT_WAIT_S"),
    )


def _model(value: str | None) -> str | None:
    return None if value is None else value.strip()


def _seconds(value: str | None, var: str) -> float:
    if value is None or not value.strip():
        return DEFAULT_EXIT_WAIT_S
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ValueError(f"{var}: expected a number of seconds, got {value!r}") from exc
    if seconds < 0:
        raise ValueError(f"{var}: must not be negative")
    return seconds


def _split(value: str | None, var: str) -> list[str]:
    if not value:
        return []
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"{var}: cannot parse {value!r}: {exc}") from exc


def _parse_env_pairs(value: str | None, var: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in _split(value, var):
        key, sep, val = item.partition("=")
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ValueError(f"{var}: expected KEY=VALUE pairs, got {item!r}")
        pairs[key] = val
    return pairs
