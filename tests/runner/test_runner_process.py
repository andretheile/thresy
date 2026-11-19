"""Subprocess machinery: streaming, timeouts, cancellation, process-group cleanup.

Uses a tiny fake CLI (a Python script generated per test) driven by a
minimal test adapter, so these tests do not depend on any real harness.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from runner_testkit import (
    collect,
    group_alive,
    pid_alive,
    read_json,
    run_collect,
    spec_env,
    wait_until,
    write_fake_cli,
)

from thresy.runner import Final, HarnessConfig, Progress, RunEvent, RunRequest, Started
from thresy.runner import process as process_mod
from thresy.runner.cli_base import CliRunner, Invocation, StreamParser
from thresy.runner.core import RunnerCapabilities
from thresy.runner.process import ChildProcess, ProcessExit

GRACE = 0.3


class LineParser(StreamParser):
    """{"session": id} -> Started, {"text": t} -> Progress, {"result": t} -> done."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.final: str | None = None

    def feed(self, obj: dict[str, Any]) -> list[RunEvent]:
        if "session" in obj:
            self.session_id = obj["session"]
            return [Started(obj["session"])]
        if "text" in obj:
            self.last_text = obj["text"]
            return [Progress("text", obj["text"])]
        if "result" in obj:
            self.final = obj["result"]
        return []

    def finish(self, exit: ProcessExit):
        if exit.cancelled:
            return self.cancelled_result()
        if self.final is not None:
            return self.result(ok=True, text=self.final)
        return self.failure_from_exit(exit)


class LineRunner(CliRunner):
    name = "line"
    capabilities = RunnerCapabilities(True, True, False)
    default_binaries = ("fake-cli-that-does-not-exist",)

    def build_invocation(self, req, binary, model):
        return Invocation([binary, "--some-flag"], stdin=req.prompt.encode())

    def new_parser(self, req, model):
        return LineParser(self.name, req, model)

    async def check_auth(self, binary, env):
        return True, "ok", []


def make(tmp_path: Path, **spec: Any) -> tuple[LineRunner, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "record.json"
    spec.setdefault("record", str(record))
    config = HarnessConfig(binary=str(write_fake_cli(bin_dir)), env=spec_env(**spec))
    return LineRunner(config, kill_grace_s=GRACE), record


def req(tmp_path: Path, **kw: Any) -> RunRequest:
    kw.setdefault("timeout_s", 20)
    return RunRequest(prompt=kw.pop("prompt", "hello"), cwd=tmp_path, **kw)


def out(**obj: Any) -> dict[str, str]:
    return {"out": json.dumps(obj)}


def assert_group_gone(record: Path, *extra_pids: int) -> None:
    info = read_json(record)
    assert wait_until(lambda: not group_alive(info["pgid"])), "process group still alive"
    for pid in (info["pid"], *extra_pids):
        assert wait_until(lambda pid=pid: not pid_alive(pid)), f"pid {pid} still alive"


def test_normal_stream(tmp_path):
    runner, record = make(
        tmp_path,
        env_keys=["FROM_REQUEST", "HOME"],
        steps=[out(session="s-1"), out(text="working"), out(result="done")],
    )
    events, result = run_collect(runner.run(req(tmp_path, env={"FROM_REQUEST": "yes"})))
    assert events[:2] == [Started("s-1"), Progress("text", "working")]
    assert result.ok and result.text == "done" and result.session_id == "s-1"
    seen = read_json(record)
    assert seen["argv"] == ["--some-flag"]
    assert seen["stdin"] == "hello"
    assert seen["env"]["FROM_REQUEST"] == "yes"
    assert seen["env"]["HOME"]  # the parent environment is inherited
    assert seen["pgid"] == seen["pid"]


def test_garbage_lines(tmp_path):
    runner, _ = make(
        tmp_path,
        steps=[
            {"out": "not json at all"},
            out(session="s-2"),
            {"out": "{broken"},
            {"out": "42"},
            out(result="fine"),
        ],
    )
    _, result = run_collect(runner.run(req(tmp_path)))
    assert result.ok and result.text == "fine"


def test_nonzero_exit_without_result(tmp_path):
    runner, _ = make(
        tmp_path, steps=[out(session="s-3"), {"out": "garbage"}, {"err": "it broke\n"}], exit=3
    )
    _, result = run_collect(runner.run(req(tmp_path)))
    assert not result.ok and result.error_kind == "crash"
    assert "exit code 3" in result.error_detail
    assert "it broke" in result.error_detail
    assert "garbage" in result.error_detail
    assert result.session_id == "s-3"


def test_timeout_stops_process_group(tmp_path):
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path, steps=[{"spawn": str(grandchild)}, out(session="s-4"), {"sleep": 600}]
    )
    start = time.monotonic()
    events, result = run_collect(runner.run(req(tmp_path, timeout_s=1.0)))
    assert time.monotonic() - start < 8
    assert events[0] == Started("s-4")
    assert result.error_kind == "timeout" and not result.ok
    assert_group_gone(record, int(grandchild.read_text()))


def test_escalates_to_sigkill(tmp_path):
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path,
        ignore_sigint=True,
        ignore_sigterm=True,
        steps=[{"spawn": str(grandchild)}, out(session="s-5"), {"sleep": 600}],
    )
    start = time.monotonic()
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=0.5)))
    elapsed = time.monotonic() - start
    assert result.error_kind == "timeout"
    assert elapsed >= 0.5 + 2 * GRACE  # waited out SIGINT and SIGTERM
    assert_group_gone(record, int(grandchild.read_text()))


def test_cancel_via_handle(tmp_path):
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path, steps=[{"spawn": str(grandchild)}, out(session="s-6"), {"sleep": 600}]
    )

    async def scenario():
        handle = runner.run(req(tmp_path))
        events = []
        async for event in handle:
            events.append(event)
            if isinstance(event, Started):
                handle.cancel("user said stop")
        return events

    events = asyncio.run(scenario())
    assert len([e for e in events if isinstance(e, Final)]) == 1
    result = events[-1].result
    assert result.error_kind == "other"
    assert result.error_detail == "cancelled: user said stop"
    assert result.session_id == "s-6"
    assert_group_gone(record, int(grandchild.read_text()))


def test_consumer_task_cancellation_stops_group(tmp_path):
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path, steps=[{"spawn": str(grandchild)}, out(session="s-7"), {"sleep": 600}]
    )

    async def scenario():
        started = asyncio.Event()

        async def consume():
            async for event in runner.run(req(tmp_path)):
                if isinstance(event, Started):
                    started.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert_group_gone(record, int(grandchild.read_text()))


def test_leaving_async_with_early_stops_group(tmp_path):
    runner, record = make(tmp_path, steps=[out(session="s-8"), {"sleep": 600}])

    async def scenario():
        async with runner.run(req(tmp_path)) as run:
            async for event in run:
                if isinstance(event, Started):
                    break

    asyncio.run(scenario())
    assert_group_gone(record)


def test_stderr_flood_does_not_deadlock(tmp_path):
    runner, _ = make(
        tmp_path,
        steps=[{"err_bytes": 3_000_000}, out(session="s-9"), out(result="survived")],
        exit=0,
    )
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=30)))
    assert result.ok and result.text == "survived"


def test_large_prompt_on_stdin(tmp_path):
    prompt = "x" * 2_000_000
    runner, record = make(tmp_path, steps=[out(result="read it")])
    _, result = run_collect(runner.run(req(tmp_path, prompt=prompt, timeout_s=30)))
    assert result.ok
    assert len(read_json(record)["stdin"]) == len(prompt)


def test_overlong_line_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(process_mod, "MAX_LINE_BYTES", 1000)
    runner, _ = make(tmp_path, steps=[{"out_long": 50_000}, out(text="after"), out(result="ok")])
    events, result = run_collect(runner.run(req(tmp_path)))
    assert result.ok and Progress("text", "after") in events


def test_child_process_reports_overlong_and_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(process_mod, "MAX_LINE_BYTES", 1000)
    script = write_fake_cli(tmp_path)
    env = {
        **os.environ,
        **spec_env(steps=[{"out_long": 5000}, {"out": "short"}, {"err": "warn"}], exit=4),
    }

    async def scenario():
        child = ChildProcess([str(script)], cwd=tmp_path, env=env, grace_s=GRACE)
        async with child:
            lines = [line async for line in child.lines(timeout_s=10)]
        return lines, child.exit

    lines, exit = asyncio.run(scenario())
    assert lines == ["short"]
    assert exit.returncode == 4 and exit.overlong_lines == 1 and exit.stderr == "warn"
    assert not exit.timed_out and not exit.cancelled


def test_leftover_processes_after_exit_are_stopped(tmp_path):
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(tmp_path, steps=[{"spawn": str(grandchild)}, out(result="bye")])
    _, result = run_collect(runner.run(req(tmp_path)))
    assert result.ok
    assert_group_gone(record, int(grandchild.read_text()))


def test_result_then_stdout_held_open(tmp_path):
    # A background process keeps stdout open after the CLI exits: the run ends
    # at the timeout, keeps the answer, and the group is stopped.
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path, steps=[{"spawn": str(grandchild), "hold_stdout": True}, out(result="answer")]
    )
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=1.0)))
    assert result.ok and result.text == "answer"
    assert_group_gone(record, int(grandchild.read_text()))


def test_missing_binary(tmp_path):
    runner = LineRunner(HarnessConfig(binary=str(tmp_path / "missing")))
    events, result = run_collect(runner.run(req(tmp_path)))
    assert len(events) == 1
    assert result.error_kind == "other" and "not found" in result.error_detail


def test_binary_not_on_path(tmp_path):
    runner = LineRunner()
    _, result = run_collect(runner.run(req(tmp_path)))
    assert "fake-cli-that-does-not-exist" in result.error_detail


def test_missing_working_directory(tmp_path):
    runner, _ = make(tmp_path, steps=[])
    _, result = run_collect(runner.run(req(tmp_path / "nope")))
    assert result.error_kind == "other" and "working directory" in result.error_detail


def test_result_helper_collects(tmp_path):
    runner, _ = make(tmp_path, steps=[out(session="s-10"), out(result="via result()")])

    async def scenario():
        return await runner.run(req(tmp_path)).result()

    assert asyncio.run(scenario()).text == "via result()"


def test_internal_parser_error_becomes_final(tmp_path):
    class BrokenParser(LineParser):
        def feed(self, obj):
            raise RuntimeError("parser bug")

    class BrokenRunner(LineRunner):
        def new_parser(self, req, model):
            return BrokenParser(self.name, req, model)

    runner, record = make(tmp_path, steps=[out(session="x"), {"sleep": 600}])
    broken = BrokenRunner(runner.config, kill_grace_s=GRACE)

    async def scenario():
        return await collect(broken.run(req(tmp_path)))

    _, result = asyncio.run(scenario())
    assert result.error_kind == "other" and "parser bug" in result.error_detail
    assert_group_gone(record)
