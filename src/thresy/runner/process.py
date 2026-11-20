"""Subprocess machinery shared by the CLI adapters (POSIX only).

The child runs in its own session and therefore its own process group, so the
CLI and everything it starts (shell commands, sub-agents) can be stopped
together. stdout is read line by line; stderr is drained concurrently into a
bounded tail so a chatty child can never block on a full pipe.

With a UserSwitch (see thresy.runner.isolation) the child is started as another
Unix user through a setuid helper, and signals to its group go through the same
helper.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .isolation import SignalOutcome, UserSwitch

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
"""Longer stdout lines are dropped (counted in ProcessExit.overlong_lines)."""
STDERR_TAIL_BYTES = 16 * 1024
DEFAULT_GRACE_S = 3.0
"""Time a signal gets to work before the next, harsher one is sent."""
KILL_WAIT_S = 2.0
POLL_S = 0.05

_BACKGROUND: set[asyncio.Future] = set()


class SpawnError(Exception):
    """The child process could not be started."""


@dataclass(frozen=True)
class ProcessExit:
    returncode: int | None
    """Exit status; negative for "killed by signal"; None if it could not be reaped."""
    timed_out: bool
    cancelled: bool
    stderr: str
    """The last STDERR_TAIL_BYTES of stderr, decoded."""
    overlong_lines: int = 0
    group_cleanup: bool = False
    """True if processes of the child's group had to be stopped after the
    child itself had exited (stray background processes)."""
    lingered: bool = False
    """True if the child was stopped because it did not exit within the time
    given by ChildProcess.expect_exit() (its final result had already arrived)."""


class ChildProcess:
    """One CLI process in its own process group.

    Use as an async context manager; leaving the block always stops the whole
    process group (if anything is left) and fills `exit`::

        async with ChildProcess(argv, cwd=cwd, env=env) as child:
            async for line in child.lines(timeout_s=60):
                ...
        print(child.exit)
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str,
        env: Mapping[str, str],
        stdin_data: bytes | None = None,
        grace_s: float = DEFAULT_GRACE_S,
        switch: UserSwitch | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env)
        self.stdin_data = stdin_data
        self.grace_s = grace_s
        self.switch = switch
        """Start the child as another user (None: as this process's user)."""
        self.exit: ProcessExit | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_buf = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdin_task: asyncio.Task[None] | None = None
        self._timed_out = False
        self._cancelled = False
        self._lingered = False
        self._overlong = 0
        self._deadline = float("inf")
        self._exit_deadline: float | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def expect_exit(self, within_s: float) -> None:
        """The CLI has reported its final result: from now on it gets at most
        `within_s` seconds to exit before `lines()` ends and the group is stopped."""
        target = asyncio.get_running_loop().time() + max(within_s, 0.0)
        if self._exit_deadline is None or target < self._exit_deadline:
            self._exit_deadline = target

    def _next_deadline(self) -> tuple[float, bool]:
        """(absolute loop time, whether it is the exit deadline)."""
        if self._exit_deadline is not None and self._exit_deadline < self._deadline:
            return self._exit_deadline, True
        return self._deadline, False

    def _expired(self, is_exit_deadline: bool) -> None:
        if is_exit_deadline:
            self._lingered = True
        else:
            self._timed_out = True

    async def __aenter__(self) -> ChildProcess:
        argv, env, extra = self.argv, self.env, {}
        if self.switch is not None:
            argv = self.switch.wrap(argv)
            env = self.switch.environment(env)
            extra["umask"] = self.switch.umask
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                env=env,
                stdin=(
                    asyncio.subprocess.PIPE
                    if self.stdin_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                **extra,
            )
        except OSError as exc:
            raise SpawnError(f"{exc.strerror or exc} ({argv[0]})") from exc
        except ValueError as exc:  # for example a NUL byte in an argument
            raise SpawnError(f"invalid command line: {exc}") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        if self.stdin_data is not None:
            self._stdin_task = asyncio.create_task(self._feed_stdin(self.stdin_data))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
            self._cancelled = True
        # Shielded (and strongly referenced): a second cancellation of the
        # caller must not leave processes behind.
        task = asyncio.ensure_future(self._finish())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        await asyncio.shield(task)

    async def lines(
        self, *, timeout_s: float, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[str]:
        """Yield stdout lines (without newline) until EOF and process exit,
        the deadline, the exit deadline set by expect_exit(), or `cancel` being
        set. Afterwards `timed_out`, `lingered` or `cancelled` tell which; the
        caller then leaves the context manager, which stops the process group."""
        assert self._proc is not None and self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + max(timeout_s, 0.0)
        cancel_task = loop.create_task(cancel.wait()) if cancel is not None else None
        read_task: asyncio.Task[bytes] | None = None
        buf = bytearray()
        skipping = False
        try:
            while True:
                if read_task is None:
                    read_task = loop.create_task(self._proc.stdout.read(CHUNK_SIZE))
                deadline, is_exit = self._next_deadline()
                outcome = await _wait_first(read_task, cancel_task, deadline - loop.time())
                if outcome == "cancel":
                    self._cancelled = True
                    return
                if outcome == "timeout":
                    self._expired(is_exit)
                    return
                chunk = read_task.result()
                read_task = None
                if not chunk:
                    if buf and not skipping:
                        yield _decode(buf)
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > MAX_LINE_BYTES:
                            if not skipping:
                                self._overlong += 1
                                log.warning(
                                    "dropping stdout line longer than %d bytes", MAX_LINE_BYTES
                                )
                            skipping = True
                            buf.clear()
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if skipping:
                        skipping = False
                        continue
                    if len(line) > MAX_LINE_BYTES:
                        self._overlong += 1
                        log.warning("dropping stdout line longer than %d bytes", MAX_LINE_BYTES)
                        continue
                    yield _decode(line)
            # stdout is closed; the process should exit now.
            wait_task = loop.create_task(self._proc.wait())
            deadline, is_exit = self._next_deadline()
            try:
                outcome = await _wait_first(wait_task, cancel_task, deadline - loop.time())
            finally:
                if not wait_task.done():
                    wait_task.cancel()
            if outcome == "cancel":
                self._cancelled = True
            elif outcome == "timeout":
                self._expired(is_exit)
        finally:
            for task in (read_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()

    @property
    def timed_out(self) -> bool:
        return self._timed_out

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def lingered(self) -> bool:
        return self._lingered

    async def _finish(self) -> None:
        proc = self._proc
        if proc is None:
            return
        group_cleanup = False
        if proc.returncode is None:
            await self._stop_group()
        elif self._group_alive():
            # The CLI exited but left processes in its group behind.
            group_cleanup = True
            log.info("stopping leftover processes of group %d", proc.pid)
            await self._stop_group()
        if self._stdin_task is not None and not self._stdin_task.done():
            self._stdin_task.cancel()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, KILL_WAIT_S)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # a broken pipe must not hide the run's outcome
                log.debug("stderr reader failed", exc_info=True)
        self.exit = ProcessExit(
            returncode=proc.returncode,
            timed_out=self._timed_out,
            cancelled=self._cancelled,
            stderr=_decode(self._stderr_buf[-STDERR_TAIL_BYTES:]),
            overlong_lines=self._overlong,
            group_cleanup=group_cleanup,
            lingered=self._lingered,
        )

    def _group_alive(self) -> bool:
        assert self._proc is not None
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _signal_group(self, pgid: int, sig: int) -> SignalOutcome:
        if self.switch is not None:
            return await self.switch.signal_group(pgid, sig)
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            log.warning("not permitted to signal process group %d", pgid)
            return "failed"
        return "sent"

    async def _stop_group(self) -> None:
        """SIGINT, then SIGTERM, then SIGKILL to the whole group, each with a
        grace period, until the CLI has exited and the group is empty."""
        proc = self._proc
        assert proc is not None
        pgid = proc.pid
        for sig, wait_s in (
            (signal.SIGINT, self.grace_s),
            (signal.SIGTERM, self.grace_s),
            (signal.SIGKILL, KILL_WAIT_S),
        ):
            outcome = await self._signal_group(pgid, sig)
            if outcome == "gone":
                break
            if outcome == "failed" and self.switch is None:
                break  # without a helper there is nothing else to try
            if await self._wait_gone(wait_s):
                break
        else:
            log.warning("process group %d still has members after SIGKILL", pgid)
        if proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), KILL_WAIT_S)

    async def _wait_gone(self, wait_s: float) -> bool:
        proc = self._proc
        assert proc is not None
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_s
        while True:
            if proc.returncode is not None and not self._group_alive():
                return True
            if loop.time() >= end:
                return False
            await asyncio.sleep(POLL_S)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while chunk := await self._proc.stderr.read(CHUNK_SIZE):
            self._stderr_buf.extend(chunk)
            if len(self._stderr_buf) > 2 * STDERR_TAIL_BYTES:
                del self._stderr_buf[:-STDERR_TAIL_BYTES]

    async def _feed_stdin(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        stdin = self._proc.stdin
        try:
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("child closed stdin before reading all input")
        finally:
            with contextlib.suppress(Exception):
                stdin.close()
                await stdin.wait_closed()


async def run_capture(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | str,
    timeout_s: float = 20.0,
    grace_s: float = 1.0,
    switch: UserSwitch | None = None,
) -> tuple[ProcessExit, str]:
    """Run a short command (for example `--version`) and return its exit info
    and stdout. Raises SpawnError if it cannot be started."""
    out: list[str] = []
    child = ChildProcess(argv, cwd=cwd, env=env, grace_s=grace_s, switch=switch)
    async with child:
        async for line in child.lines(timeout_s=timeout_s):
            out.append(line)
    assert child.exit is not None
    return child.exit, "\n".join(out)


async def _wait_first(
    task: asyncio.Future, cancel_task: asyncio.Future | None, remaining: float
) -> str:
    """Wait for `task`; return "done", "cancel" or "timeout"."""
    if remaining <= 0:
        return "done" if task.done() else "timeout"
    waiters: set[asyncio.Future] = {task}
    if cancel_task is not None:
        waiters.add(cancel_task)
    done, _ = await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
    if cancel_task is not None and cancel_task in done:
        return "cancel"
    if task in done:
        return "done"
    return "timeout"


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace").rstrip("\r")
