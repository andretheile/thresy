"""The real user switch: the setuid helper, a second user, real process groups.

Runs only where THRESY_TEST_AGENT_USER names the agent user and the helper is installed, which
is the test image built by docker/isolation-test.sh (the bridge's users, helper and container
settings). Everywhere else these tests are skipped; tests/runner/test_isolation.py covers the
runner's side with a stand-in helper.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from thresy.runner import Final, HarnessConfig, RunnerConfig, RunRequest, Started
from thresy.runner.claude import ClaudeRunner
from thresy.runner.isolation import UserSwitch, switch_from_env
from thresy.runner.process import ChildProcess, run_capture

from .runner_testkit import (
    group_alive,
    pid_alive,
    read_json,
    run_collect,
    spec_env,
    wait_until,
    write_fake_cli,
)
from .test_runner_process import GRACE, LineRunner, out

AGENT_USER = os.environ.get("THRESY_TEST_AGENT_USER", "")
HELPER = os.environ.get("THRESY_TEST_AGENT_HELPER", "/usr/local/libexec/thresy/run-as-agent")

pytestmark = pytest.mark.skipif(
    not AGENT_USER or not sys.platform.startswith("linux") or not Path(HELPER).exists(),
    reason="needs the agent user and the setuid helper (run docker/isolation-test.sh)",
)


@pytest.fixture
def switch() -> UserSwitch:
    found = switch_from_env({"COACH_AGENT_USER": AGENT_USER, "COACH_AGENT_USER_HELPER": HELPER})
    assert found is not None
    return found


@pytest.fixture
def shared(switch: UserSwitch) -> Iterator[Path]:
    """A directory both users can use, like the workspace (agent group, setgid, 2770)."""
    path = Path(tempfile.mkdtemp(prefix="thresy-shared-", dir="/tmp"))
    os.chown(path, -1, switch.gid)
    path.chmod(0o2770)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make(shared: Path, switch: UserSwitch, **spec: Any) -> tuple[LineRunner, Path]:
    bin_dir = shared / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = shared / "record.json"
    spec.setdefault("record", str(record))
    harness = HarnessConfig(binary=str(write_fake_cli(bin_dir)), env=spec_env(**spec))
    return LineRunner(harness, kill_grace_s=GRACE, switch=switch), record


def req(cwd: Path, **kw: Any) -> RunRequest:
    kw.setdefault("timeout_s", 20)
    return RunRequest(prompt=kw.pop("prompt", "hello"), cwd=cwd, **kw)


def assert_all_gone(record: Path, *extra_pids: int) -> None:
    info = read_json(record)
    assert wait_until(lambda: not group_alive(info["pgid"])), "process group still alive"
    for pid in (info["pid"], *extra_pids):
        assert wait_until(lambda pid=pid: not pid_alive(pid)), f"pid {pid} still alive"


def run_as_agent(switch: UserSwitch, cwd: Path, code: str, *args: str) -> tuple[int | None, str]:
    async def go():
        exit, stdout = await run_capture(
            [sys.executable, "-I", "-S", "-c", code, *args],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            cwd=cwd,
            switch=switch,
        )
        return exit.returncode, stdout + exit.stderr

    return asyncio.run(go())


# -- identity and environment -------------------------------------------------------------------


def test_cli_runs_as_the_agent_with_the_allow_listed_environment(shared, switch):
    runner, record = make(
        shared,
        switch,
        env_keys=["HOME", "USER", "LOGNAME", "FROM_REQUEST", "COACH_SECRET_OF_THE_BRIDGE"],
        steps=[out(session="s-1"), out(result="done")],
    )
    os.environ["COACH_SECRET_OF_THE_BRIDGE"] = "must-not-pass"
    try:
        _, result = run_collect(runner.run(req(shared, env={"FROM_REQUEST": "yes"})))
    finally:
        del os.environ["COACH_SECRET_OF_THE_BRIDGE"]
    assert result.ok and result.text == "done"
    assert record.stat().st_uid == switch.uid  # written by the CLI, as the agent
    assert stat.S_IMODE(record.stat().st_mode) == 0o660  # umask 007
    assert record.stat().st_gid == switch.gid  # setgid directory
    seen = read_json(record)
    assert seen["env"] == {
        "HOME": switch.home,
        "USER": switch.user,
        "LOGNAME": switch.user,
        "FROM_REQUEST": "yes",
        "COACH_SECRET_OF_THE_BRIDGE": None,
    }
    assert seen["stdin"] == "hello"


def test_agent_cannot_reach_the_bridge(shared, switch):
    code = (
        "import json, os, sys\n"
        "parent = int(sys.argv[1])\n"
        "def attempt(f):\n"
        "    try:\n"
        "        f(); return 'allowed'\n"
        "    except PermissionError:\n"
        "        return 'denied'\n"
        "print(json.dumps({\n"
        "  'uid': os.getuid(),\n"
        "  'environ_parent': attempt(lambda: open('/proc/%d/environ' % parent, 'rb').read()),\n"
        "  'environ_pid1': attempt(lambda: open('/proc/1/environ', 'rb').read()),\n"
        "  'signal_parent': attempt(lambda: os.kill(parent, 0)),\n"
        "  'helper': attempt(lambda: os.execv(sys.argv[2], [sys.argv[2], '--', '/bin/true'])),\n"
        "}))\n"
    )
    import json

    rc, output = run_as_agent(switch, shared, code, str(os.getpid()), HELPER)
    assert rc == 0, output
    result = json.loads(output.strip().splitlines()[-1])
    assert result == {
        "uid": switch.uid,
        "environ_parent": "denied",
        "environ_pid1": "denied",
        "signal_parent": "denied",
        "helper": "denied",
    }


def test_no_new_privs_is_set(shared, switch):
    rc, output = run_as_agent(
        switch,
        shared,
        "print([l for l in open('/proc/self/status') if l.startswith('NoNewPrivs')][0])",
    )
    assert rc == 0 and "NoNewPrivs:\t1" in output, output


def test_credential_file_is_loaded_for_the_agent_only(shared, switch):
    path = switch.credentials_file
    if not os.path.lexists(path) and not _agent_can_see(switch, shared, path):
        pytest.skip("no credential file for the agent in this image")
    with pytest.raises(PermissionError):
        path.read_text()  # the bridge's user cannot read it
    runner, record = make(
        shared,
        switch,
        env_keys=["THRESY_TEST_CREDENTIAL", "THRESY_TEST_PLAIN"],
        steps=[out(result="ok")],
    )
    _, result = run_collect(runner.run(req(shared)))
    assert result.ok
    assert read_json(record)["env"] == {
        "THRESY_TEST_CREDENTIAL": "from the agent file",
        "THRESY_TEST_PLAIN": "plain",
    }


def _agent_can_see(switch: UserSwitch, cwd: Path, path: Path) -> bool:
    rc, _ = run_as_agent(
        switch, cwd, "import os, sys; sys.exit(0 if os.path.exists(sys.argv[1]) else 1)", str(path)
    )
    return rc == 0


def test_private_system_prompt_file_is_readable_by_the_agent(shared, switch):
    runner = ClaudeRunner(switch=switch)
    inv = runner.build_invocation(
        RunRequest(prompt="p", cwd=shared, system_append="extra instructions"), "claude", None
    )
    try:
        path = inv.argv[inv.argv.index("--append-system-prompt-file") + 1]
        rc, output = run_as_agent(
            switch, shared, "import sys; print(open(sys.argv[1]).read())", path
        )
        assert rc == 0 and "extra instructions" in output, output
    finally:
        inv.cleanup()


# -- the helper's command line ------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["claude"],  # no "--"
        ["--signal", "9", "1"],  # every process
        ["--signal", "9", "0"],
        ["--signal", "9", "-5"],
        ["--signal", "99", "1234"],
        ["--signal", "x", "1234"],
    ],
)
def test_helper_refuses_bad_command_lines(args):
    proc = subprocess.run([HELPER, *args], capture_output=True, text=True, env={})
    assert proc.returncode == 2, proc.stderr
    assert "usage" in proc.stderr


def test_helper_reports_a_missing_group_and_program(switch):
    gone = subprocess.run([HELPER, "--signal", "0", "2147483647"], capture_output=True, env={})
    assert gone.returncode == 3
    missing = subprocess.run([HELPER, "--", "/nonexistent/cli"], capture_output=True, text=True)
    assert missing.returncode == 127 and "cannot execute" in missing.stderr


def test_helper_is_setuid_root_for_the_bridge_group_only():
    st = Path(HELPER).stat()
    assert st.st_uid == 0
    assert stat.S_IMODE(st.st_mode) == 0o4750


# -- stopping the agent's process group ---------------------------------------------------------


def test_timeout_stops_the_agents_group(shared, switch):
    grandchild = shared / "grandchild.pid"
    runner, record = make(
        shared, switch, steps=[{"spawn": str(grandchild)}, out(session="s-2"), {"sleep": 600}]
    )
    start = time.monotonic()
    events, result = run_collect(runner.run(req(shared, timeout_s=1.0)))
    assert time.monotonic() - start < 8
    assert events[0] == Started("s-2")
    assert result.error_kind == "timeout"
    assert_all_gone(record, int(grandchild.read_text()))


def test_escalates_to_sigkill_across_users(shared, switch):
    grandchild = shared / "grandchild.pid"
    runner, record = make(
        shared,
        switch,
        ignore_sigint=True,
        ignore_sigterm=True,
        steps=[{"spawn": str(grandchild)}, out(session="s-3"), {"sleep": 600}],
    )
    start = time.monotonic()
    _, result = run_collect(runner.run(req(shared, timeout_s=0.5)))
    elapsed = time.monotonic() - start
    assert result.error_kind == "timeout"
    assert elapsed >= 0.5 + 2 * GRACE
    assert_all_gone(record, int(grandchild.read_text()))


def test_cancel_stops_the_agents_group(shared, switch):
    grandchild = shared / "grandchild.pid"
    runner, record = make(
        shared, switch, steps=[{"spawn": str(grandchild)}, out(session="s-4"), {"sleep": 600}]
    )

    async def scenario():
        handle = runner.run(req(shared))
        events = []
        async for event in handle:
            events.append(event)
            if isinstance(event, Started):
                handle.cancel("stop")
        return events

    events = asyncio.run(scenario())
    final = [e for e in events if isinstance(e, Final)]
    assert len(final) == 1 and final[0].result.error_kind == "cancelled"
    assert_all_gone(record, int(grandchild.read_text()))


def test_consumer_cancellation_stops_the_agents_group(shared, switch):
    grandchild = shared / "grandchild.pid"
    runner, record = make(
        shared, switch, steps=[{"spawn": str(grandchild)}, out(session="s-5"), {"sleep": 600}]
    )

    async def scenario():
        async def consume():
            async with runner.run(req(shared)) as handle:
                async for _ in handle:
                    pass

        task = asyncio.create_task(consume())
        assert await asyncio.to_thread(wait_until, grandchild.exists, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert_all_gone(record, int(grandchild.read_text()))


def test_leftover_processes_after_exit_are_stopped(shared, switch):
    grandchild = shared / "grandchild.pid"
    runner, record = make(
        shared, switch, steps=[{"spawn": str(grandchild)}, out(result="done")], exit=0
    )
    _, result = run_collect(runner.run(req(shared)))
    assert result.ok
    assert_all_gone(record, int(grandchild.read_text()))


def test_lingering_cli_after_result(shared, switch):
    runner, record = make(shared, switch, steps=[out(result="answer"), {"sleep": 600}])
    runner.config = HarnessConfig(
        binary=runner.config.binary, env=runner.config.env, exit_wait_s=0.3
    )
    start = time.monotonic()
    _, result = run_collect(runner.run(req(shared)))
    assert result.ok and result.text == "answer"
    assert time.monotonic() - start < 5
    assert_all_gone(record)


def test_child_process_direct_stop(shared, switch):
    script = shared / "linger.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(600)\n"
    )
    script.chmod(0o640)

    async def scenario():
        child = ChildProcess(
            [sys.executable, str(script)], cwd=shared, env={}, grace_s=0.3, switch=switch
        )
        async with child:
            async for line in child.lines(timeout_s=10):
                if line == "ready":
                    break
        return child

    child = asyncio.run(scenario())
    assert child.exit is not None and child.exit.returncode == -15  # SIGTERM after SIGINT
    assert child.pid is not None and not group_alive(child.pid)


# -- the self-test ------------------------------------------------------------------------------


def test_self_test_passes(shared, switch, capsys):
    from thresy.runner import selftest

    workspace = shared / "workspace"
    for path in (workspace, workspace / "inbox", workspace / "out"):
        path.mkdir()
        path.chmod(0o2770)
    environ = {**os.environ, "COACH_WORKSPACE_DIR": str(workspace), "COACH_DATA_DIR": "/data"}
    report = selftest.run(RunnerConfig(user_switch=switch), environ=environ)
    with capsys.disabled():
        print("\n" + report.text())
    assert report.ok, report.text()
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["runs as another user"] == "PASS"
    assert statuses["reads other processes' environment"] == "PASS"
    assert statuses["stops processes that ignore SIGINT and SIGTERM"] == "PASS"
