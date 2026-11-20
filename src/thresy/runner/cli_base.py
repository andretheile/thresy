"""Base classes for adapters that drive a coding-agent CLI as a subprocess."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from .classify import classify_text, truncate
from .config import HarnessConfig
from .core import (
    ErrorKind,
    Health,
    ModelTier,
    Progress,
    RunEvent,
    RunnerCapabilities,
    RunRequest,
    RunResult,
)
from .environment import allowed_environment
from .handle import RunContext, RunHandle
from .isolation import UserSwitch
from .process import DEFAULT_GRACE_S, ChildProcess, ProcessExit, SpawnError, run_capture

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_VERSION_RE = re.compile(r"\d+\.\d+(\.\d+)?([-+.\w]*)?")
MAX_DETAIL = 2000
RETRY_WITHOUT_EFFORT: tuple[ErrorKind, ...] = ("other", "crash")
"""A run with an effort level that fails like this before any output is tried once without."""


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    stdin: bytes | None = None
    temp_dirs: tuple[Path, ...] = ()
    """Private temporary directories (for example a system-prompt file) that
    are removed when the run is over."""

    def cleanup(self) -> None:
        for path in self.temp_dirs:
            shutil.rmtree(path, ignore_errors=True)


def private_file(content: str, name: str, *, group: int | None = None) -> tuple[Path, Path]:
    """Write `content` to a new file only this user can read; returns (dir, file).

    Used instead of command-line arguments, which every user on the host can
    read via `ps`. With `group` (the agent user's group when the agent runs as
    its own user) members of that group may read it as well."""
    directory = Path(tempfile.mkdtemp(prefix="thresy-runner-"))  # mode 0700
    path = directory / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        if group is not None:
            os.chown(path, -1, group)
            path.chmod(0o640)
            os.chown(directory, -1, group)
            directory.chmod(0o750)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory, path


def prepend_system(prompt: str, system_append: str | None) -> str:
    """The contract's fallback for harnesses without a usable system-prompt route."""
    if not system_append or not system_append.strip():
        return prompt
    return f"{system_append.strip()}\n\n---\n\n{prompt}"


class StreamParser(ABC):
    """Turns one run's stdout lines into events and, at the end, a RunResult.

    Subclasses implement `feed` (one decoded JSON object) and `finish`, and set
    `terminal_seen` once the harness has reported its final result, so the
    runner stops waiting for a process that does not exit.
    """

    def __init__(self, runner: str, req: RunRequest, model: str | None) -> None:
        self.runner = runner
        self.req = req
        self.model = model
        self.session_id: str | None = None
        self.last_text = ""
        self.cancel_reason: str | None = None
        self.terminal_seen = False
        self.garbage: deque[str] = deque(maxlen=5)
        self.garbage_count = 0
        self.credential_expires_at: datetime | None = None
        """Set by a parser whose harness reports when its credential ends; goes into every
        result built afterwards (RunResult.credential_expires_at)."""

    def feed_line(self, line: str) -> list[RunEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            self.garbage_count += 1
            self.garbage.append(truncate(line, 300))
            log.debug("%s: ignoring non-JSON stdout line: %s", self.runner, truncate(line, 200))
            return []
        return self.feed(obj)

    @abstractmethod
    def feed(self, obj: dict[str, Any]) -> list[RunEvent]: ...

    @abstractmethod
    def finish(self, exit: ProcessExit) -> RunResult: ...

    # -- helpers for subclasses -------------------------------------------------

    def result(
        self,
        *,
        ok: bool,
        text: str | None = None,
        error_kind: ErrorKind | None = None,
        error_detail: str | None = None,
        cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> RunResult:
        return RunResult(
            ok=ok,
            text=self.last_text if text is None else text,
            session_id=self.session_id,
            error_kind=None if ok else (error_kind or "other"),
            error_detail=None if ok else _clip(error_detail),
            cost_usd=cost_usd,
            usage=usage,
            duration_s=0.0,  # filled in by RunHandle
            runner=self.runner,
            model=self.model,
            credential_expires_at=self.credential_expires_at,
        )

    def error(self, kind: ErrorKind, detail: str, **kw: Any) -> RunResult:
        return self.result(ok=False, error_kind=kind, error_detail=detail, **kw)

    def cancelled_result(self, **kw: Any) -> RunResult:
        return self.error("cancelled", self.cancel_reason or "cancelled", **kw)

    def failure_from_exit(self, exit: ProcessExit, *messages: str | None, **kw: Any) -> RunResult:
        """Result for a run that ended without a usable terminal event."""
        if exit.cancelled:
            return self.cancelled_result(**kw)
        if exit.timed_out:
            known = [m for m in messages if m]
            note = f" (last error: {truncate(known[-1], 200)})" if known else ""
            return self.error("timeout", f"no result within {self.req.timeout_s:g}s{note}", **kw)
        garbage = "\n".join(self.garbage)
        kind = classify_text(*messages, exit.stderr, garbage) or "crash"
        parts = []
        if exit.returncode is None:
            parts.append("process could not be reaped")
        elif exit.returncode < 0:
            parts.append(f"killed by signal {-exit.returncode}")
        elif exit.returncode != 0:
            parts.append(f"exit code {exit.returncode}")
        else:
            parts.append("exited without a result")
        parts.extend(m for m in messages if m)
        if exit.stderr.strip():
            parts.append("stderr: " + exit.stderr.strip()[-800:])
        if self.garbage_count:
            parts.append(f"{self.garbage_count} non-JSON stdout line(s), last: {self.garbage[-1]}")
        if exit.overlong_lines:
            parts.append(f"{exit.overlong_lines} overlong stdout line(s) dropped")
        return self.error(kind, "; ".join(parts), **kw)


class CliRunner(ABC):
    """An AgentRunner backed by a CLI. Subclasses fill in the class variables
    and implement `build_invocation`, `new_parser` and `check_auth`."""

    name: ClassVar[str]
    capabilities: ClassVar[RunnerCapabilities]
    default_binaries: ClassVar[tuple[str, ...]]
    default_models: ClassVar[Mapping[str, str | None]] = {}
    default_permission_args: ClassVar[tuple[str, ...]] = ()
    base_env: ClassVar[Mapping[str, str]] = {}
    """Fixed additions to the child environment."""
    env_names: ClassVar[frozenset[str]] = frozenset()
    env_prefixes: ClassVar[tuple[str, ...]] = ()
    """Parent variables the harness reads for its own auth and configuration;
    they pass the allow-list (see thresy.runner.environment)."""
    env_blocked: ClassVar[frozenset[str]] = frozenset()
    """Parent variables that never pass, even if a prefix matches."""
    verified: ClassVar[bool] = False
    """Whether this adapter has been run against the real CLI."""

    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        kill_grace_s: float | None = None,
        switch: UserSwitch | None = None,
    ):
        self.config = config or HarnessConfig()
        self.kill_grace_s = DEFAULT_GRACE_S if kill_grace_s is None else kill_grace_s
        self.switch = switch
        """Run the CLI as another Unix user (see thresy.runner.isolation); None: as this one."""

    # -- configuration -------------------------------------------------------------

    @property
    def permission_args(self) -> tuple[str, ...]:
        if self.config.permission_args is None:
            return self.default_permission_args
        return self.config.permission_args

    def model_for(self, tier: ModelTier) -> str | None:
        return self.config.model_for(tier, self.default_models)

    def child_env(
        self, req: RunRequest | None = None, parent: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """The CLI's environment: the allow-listed part of the parent
        environment, then base_env, COACH_<NAME>_ENV and RunRequest.env."""
        env = allowed_environment(
            os.environ if parent is None else parent,
            harness_names=self.env_names,
            harness_prefixes=self.env_prefixes,
            pass_through=self.config.pass_env,
            blocked=self.env_blocked,
        )
        env.update(self.base_env)
        env.update(self.config.env)
        if req is not None:
            env.update(req.env)
        return env

    def resolve_binary(self, env: Mapping[str, str]) -> str | None:
        candidates = (self.config.binary,) if self.config.binary else self.default_binaries
        for candidate in candidates:
            if os.sep in candidate:
                path = Path(candidate).expanduser()
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
                continue
            found = shutil.which(candidate, path=env.get("PATH"))
            if found:
                return found
        return None

    @property
    def private_group(self) -> int | None:
        """Group for private files the CLI must read (the agent user's, when switching)."""
        return self.switch.gid if self.switch is not None else None

    def binary_names(self) -> str:
        return ", ".join((self.config.binary,) if self.config.binary else self.default_binaries)

    # -- running -------------------------------------------------------------------

    def run(self, req: RunRequest) -> RunHandle:
        return RunHandle(self.name, req, lambda ctx: self._drive(req, ctx))

    @abstractmethod
    def build_invocation(self, req: RunRequest, binary: str, model: str | None) -> Invocation: ...

    @abstractmethod
    def new_parser(self, req: RunRequest, model: str | None) -> StreamParser: ...

    async def _drive(self, req: RunRequest, ctx: RunContext) -> RunResult:
        model = self.model_for(req.model_tier)
        if req.effort is not None and not self.capabilities.supports_effort:
            log.debug("%s: no effort setting in this CLI; ignoring %s", self.name, req.effort)
            req = replace(req, effort=None)
        cwd = Path(req.cwd)
        if not cwd.is_dir():
            return self.new_parser(req, model).error(
                "other", f"working directory does not exist: {cwd}"
            )
        env = self.child_env(req)
        binary = self.resolve_binary(env)
        if binary is None:
            return self.new_parser(req, model).error(
                "other", f"{self.name} CLI not found (looked for {self.binary_names()})"
            )
        clock = time.monotonic()
        result, progressed = await self._attempt(req, ctx, binary, model, env)
        if (
            req.effort is None
            or result.ok
            or progressed
            or result.error_kind not in RETRY_WITHOUT_EFFORT
            or ctx.cancel_event.is_set()
        ):
            return result
        # A CLI too old for the level, or a model that rejects it instead of lowering it, must
        # not cost the answer: the run did nothing yet, so try once more at the default.
        remaining = req.timeout_s - (time.monotonic() - clock)
        if remaining <= 0:
            return result
        log.warning(
            "%s: the run at effort %s failed before any output (%s: %s); retrying once "
            "without an effort setting",
            self.name,
            req.effort,
            result.error_kind,
            truncate(result.error_detail or "", 300),
        )
        retry = replace(req, effort=None, timeout_s=remaining)
        result, _ = await self._attempt(retry, ctx, binary, model, env)
        return result

    async def _attempt(
        self,
        req: RunRequest,
        ctx: RunContext,
        binary: str,
        model: str | None,
        env: Mapping[str, str],
    ) -> tuple[RunResult, bool]:
        """One start of the CLI; returns the result and whether any Progress was emitted."""
        parser = self.new_parser(req, model)
        progressed = False
        try:
            inv = self.build_invocation(req, binary, model)
        except OSError as exc:
            return parser.error("other", f"could not prepare the {self.name} run: {exc}"), False
        try:
            log.debug("%s: starting %s in %s", self.name, inv.argv[0], req.cwd)
            child = ChildProcess(
                inv.argv,
                cwd=Path(req.cwd),
                env=env,
                stdin_data=inv.stdin,
                grace_s=self.kill_grace_s,
                switch=self.switch,
            )
            try:
                async with child:
                    lines = child.lines(timeout_s=req.timeout_s, cancel=ctx.cancel_event)
                    async with contextlib.aclosing(lines):
                        async for line in lines:
                            for event in parser.feed_line(line):
                                progressed = progressed or isinstance(event, Progress)
                                ctx.emit(event)  # type: ignore[arg-type]
                            if parser.terminal_seen:
                                child.expect_exit(self.config.exit_wait_s)
            except SpawnError as exc:
                return parser.error("other", f"could not start {self.name} CLI: {exc}"), False
        finally:
            inv.cleanup()
        assert child.exit is not None
        parser.cancel_reason = ctx.cancel_reason
        if child.exit.lingered:
            log.info(
                "%s: the CLI did not exit within %gs of its final result; stopped it",
                self.name,
                self.config.exit_wait_s,
            )
        elif child.exit.group_cleanup:
            log.info("%s: stopped processes left behind by the CLI", self.name)
        return parser.finish(child.exit), progressed

    # -- health ----------------------------------------------------------------------

    async def healthcheck(self) -> Health:
        env = self.child_env()
        binary = self.resolve_binary(env)
        if binary is None:
            return Health(
                runner=self.name,
                binary_found=False,
                binary_path=None,
                version=None,
                auth_ok=None,
                detail=f"{self.name} CLI not found (looked for {self.binary_names()})",
                verified_adapter=self.verified,
            )
        version = await self._version(binary, env)
        try:
            auth_ok, auth_detail, warnings = await self.check_auth(binary, env)
        except SpawnError as exc:
            auth_ok, auth_detail, warnings = None, f"auth check could not run: {exc}", []
        status = "auth ok" if auth_ok else ("auth unknown" if auth_ok is None else "auth problem")
        detail = f"{self.name} {version or '(version unknown)'}: {status}: {redact(auth_detail)}"
        warnings = [*warnings, *self.static_warnings()]
        if not self.verified:
            warnings.append("adapter not yet verified against the real CLI")
        return Health(
            runner=self.name,
            binary_found=True,
            binary_path=binary,
            version=version,
            auth_ok=auth_ok,
            detail=detail,
            verified_adapter=self.verified,
            warnings=tuple(redact(w) for w in warnings),
        )

    @abstractmethod
    async def check_auth(
        self, binary: str, env: Mapping[str, str]
    ) -> tuple[bool | None, str, list[str]]:
        """Return (auth_ok, detail, warnings) without spending model usage."""

    def static_warnings(self) -> list[str]:
        """Warnings about the adapter itself, shown by every healthcheck."""
        return []

    async def _version(self, binary: str, env: Mapping[str, str]) -> str | None:
        try:
            exit, out = await self.capture([binary, "--version"], env)
        except SpawnError:
            return None
        if exit.returncode != 0:
            return None
        match = _VERSION_RE.search(out or exit.stderr)
        return match.group(0) if match else (out.strip()[:40] or None)

    async def capture(
        self, argv: list[str], env: Mapping[str, str], timeout_s: float = 20.0
    ) -> tuple[ProcessExit, str]:
        return await run_capture(
            argv, env=env, cwd=tempfile.gettempdir(), timeout_s=timeout_s, switch=self.switch
        )


def redact(text: str) -> str:
    return _EMAIL_RE.sub("<email>", text)


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def usage_dict(raw: Mapping[str, Any] | None, mapping: Mapping[str, str]) -> dict | None:
    """Normalise a harness usage dict: `mapping` maps normalised key -> raw key."""
    if not raw:
        return None
    usage: dict[str, Any] = {}
    for key, raw_key in mapping.items():
        value = raw.get(raw_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] = value
    usage["raw"] = dict(raw)
    return usage
