"""The user switch of the subprocess layer, with a stand-in for the setuid helper.

The real helper (docker/run-as-agent.c) needs root to be installed and a second user, so these
tests use a Python script with the same command line that stays the current user: it records
how it was called, sends signals itself and loads a credential file. They check what the runner
does around the helper: the wrapped command line, the agent's identity variables, the umask,
signals routed through the helper, private files for the agent's group, and the configuration.
tests/runner/test_isolation_linux.py runs the real helper as another user (in the test image,
see docker/isolation-test.sh).
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from thresy.runner import RunnerConfig, RunRequest, get_runner
from thresy.runner.claude import ClaudeRunner
from thresy.runner.cli_base import private_file
from thresy.runner.isolation import UserSwitch, switch_from_env
from thresy.runner.process import ChildProcess

from .runner_testkit import read_json, run_collect, spec_env, write_fake_cli
from .test_runner_process import GRACE, LineRunner, assert_group_gone, out

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")

_FAKE_HELPER = """\
#!{python}
import json, os, sys

LOG = {log!r}
CREDENTIALS = {credentials!r}


def log(entry):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\\n")


if len(sys.argv) == 4 and sys.argv[1] == "--signal":
    log({{"signal": int(sys.argv[2]), "pgid": int(sys.argv[3]), "env": dict(os.environ)}})
    if {fail_signals!r}:
        sys.exit(1)
    try:
        os.killpg(int(sys.argv[3]), int(sys.argv[2]))
    except ProcessLookupError:
        sys.exit(3)
    sys.exit(0)
if len(sys.argv) < 3 or sys.argv[1] != "--":
    sys.exit(2)
log({{"exec": sys.argv[2:]}})
if CREDENTIALS and os.path.exists(CREDENTIALS):
    with open(CREDENTIALS) as fh:
        for line in fh:
            name, _, value = line.strip().partition("=")
            if name:
                os.environ[name] = value
os.execvp(sys.argv[2], sys.argv[2:])
"""


def fake_helper(tmp_path: Path, *, fail_signals: bool = False) -> tuple[UserSwitch, Path]:
    me = pwd.getpwuid(os.getuid())
    home = tmp_path / "agent-home"
    (home / ".thresy").mkdir(parents=True)
    log = tmp_path / "helper.log"
    helper = tmp_path / "run-as-agent"
    switch = UserSwitch(user=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=str(home))
    helper.write_text(
        _FAKE_HELPER.format(
            python=sys.executable,
            log=str(log),
            credentials=str(switch.credentials_file),
            fail_signals=fail_signals,
        )
    )
    helper.chmod(0o755)
    return UserSwitch(**{**switch.__dict__, "helper": str(helper)}), log


def helper_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def make(tmp_path: Path, switch: UserSwitch, **spec: Any) -> tuple[LineRunner, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "record.json"
    spec.setdefault("record", str(record))
    from thresy.runner import HarnessConfig

    harness = HarnessConfig(binary=str(write_fake_cli(bin_dir)), env=spec_env(**spec))
    return LineRunner(harness, kill_grace_s=GRACE, switch=switch), record


def req(tmp_path: Path, **kw: Any) -> RunRequest:
    kw.setdefault("timeout_s", 20)
    return RunRequest(prompt=kw.pop("prompt", "hello"), cwd=tmp_path, **kw)


# -- configuration ------------------------------------------------------------------------------


def test_switch_is_off_by_default():
    assert switch_from_env({}) is None
    assert switch_from_env({"COACH_AGENT_USER": "  "}) is None
    assert RunnerConfig.from_env({}).user_switch is None


def test_unknown_or_own_user_is_rejected():
    with pytest.raises(ValueError, match="no such user"):
        switch_from_env({"COACH_AGENT_USER": "thresy-no-such-user-xyz"})
    me = pwd.getpwuid(os.getuid()).pw_name
    with pytest.raises(ValueError, match="another user"):
        switch_from_env({"COACH_AGENT_USER": me})


def test_switch_from_env_reads_the_password_database(monkeypatch):
    other = next((p for p in pwd.getpwall() if p.pw_uid != os.getuid() and p.pw_dir), None)
    if other is None:
        pytest.skip("no second user in the password database")
    switch = switch_from_env({"COACH_AGENT_USER": other.pw_name})
    assert switch is not None
    assert (switch.uid, switch.gid, switch.home) == (other.pw_uid, other.pw_gid, other.pw_dir)
    assert switch.helper == "/usr/local/libexec/thresy/run-as-agent"
    custom = switch_from_env(
        {"COACH_AGENT_USER": other.pw_name, "COACH_AGENT_USER_HELPER": "/opt/helper"}
    )
    assert custom is not None and custom.helper == "/opt/helper"
    config = RunnerConfig.from_env({"COACH_AGENT_USER": other.pw_name})
    assert config.user_switch == switch
    runner = get_runner("claude", config)
    assert isinstance(runner, ClaudeRunner) and runner.switch == switch


def test_wrap_and_environment():
    switch = UserSwitch(user="agent", uid=10002, gid=10002, home="/home/agent", helper="/h")
    assert switch.wrap(["claude", "-p"]) == ["/h", "--", "claude", "-p"]
    env = switch.environment({"HOME": "/home/coach", "USER": "coach", "PATH": "/usr/bin"})
    assert env == {
        "HOME": "/home/agent",
        "USER": "agent",
        "LOGNAME": "agent",
        "PATH": "/usr/bin",
    }
    assert str(switch.credentials_file) == "/home/agent/.thresy/credentials.env"


# -- runs through the helper --------------------------------------------------------------------


def test_run_goes_through_the_helper(tmp_path):
    switch, log = fake_helper(tmp_path)
    (Path(switch.home) / ".thresy" / "credentials.env").write_text("FROM_CREDENTIALS=yes\n")
    runner, record = make(
        tmp_path,
        switch,
        env_keys=["HOME", "USER", "LOGNAME", "FROM_REQUEST", "FROM_CREDENTIALS"],
        steps=[out(session="s-1"), out(result="done")],
    )
    _, result = run_collect(runner.run(req(tmp_path, env={"FROM_REQUEST": "yes"})))
    assert result.ok and result.text == "done"
    seen = read_json(record)
    assert seen["argv"] == ["--some-flag"]  # the CLI's own arguments are unchanged
    assert seen["stdin"] == "hello"
    assert seen["env"] == {
        "HOME": switch.home,
        "USER": switch.user,
        "LOGNAME": switch.user,
        "FROM_REQUEST": "yes",
        "FROM_CREDENTIALS": "yes",
    }
    calls = helper_log(log)
    assert calls[0]["exec"][0] == runner.config.binary
    assert calls[0]["exec"][1:] == ["--some-flag"]


def test_child_gets_the_agent_umask(tmp_path):
    switch, _ = fake_helper(tmp_path)
    created = tmp_path / "created-by-agent"
    script = tmp_path / "touch.py"
    script.write_text(f"open({str(created)!r}, 'w').close()\n")

    async def scenario():
        child = ChildProcess(
            [sys.executable, str(script)], cwd=tmp_path, env=dict(os.environ), switch=switch
        )
        async with child:
            async for _ in child.lines(timeout_s=20):
                pass
        return child.exit

    import asyncio

    exit = asyncio.run(scenario())
    assert exit is not None and exit.returncode == 0
    assert stat.S_IMODE(created.stat().st_mode) == 0o660  # 0666 & ~007


def test_timeout_signals_the_group_through_the_helper(tmp_path):
    switch, log = fake_helper(tmp_path)
    grandchild = tmp_path / "grandchild.pid"
    runner, record = make(
        tmp_path,
        switch,
        ignore_sigint=True,
        ignore_sigterm=True,
        steps=[{"spawn": str(grandchild)}, out(session="s-2"), {"sleep": 600}],
    )
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=0.5)))
    assert result.error_kind == "timeout"
    assert_group_gone(record, int(grandchild.read_text()))
    pgid = read_json(record)["pgid"]
    signals = [c for c in helper_log(log) if "signal" in c]
    assert [(c["signal"], c["pgid"]) for c in signals] == [(2, pgid), (15, pgid), (9, pgid)]
    # The helper gets an empty environment (Python and macOS add LC_CTYPE and
    # __CF_USER_TEXT_ENCODING to the stand-in's own).
    assert all(set(c["env"]) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} for c in signals)


def test_cooperative_stop_needs_only_sigint(tmp_path):
    switch, log = fake_helper(tmp_path)
    runner, record = make(tmp_path, switch, steps=[out(session="s-3"), {"sleep": 600}])
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=0.5)))
    assert result.error_kind == "timeout"
    assert_group_gone(record)
    assert [c["signal"] for c in helper_log(log) if "signal" in c] == [2]


def test_failing_helper_still_tries_every_signal(tmp_path, caplog):
    switch, log = fake_helper(tmp_path, fail_signals=True)
    runner, record = make(tmp_path, switch, steps=[out(session="s-4"), {"sleep": 600}])
    start = time.monotonic()
    _, result = run_collect(runner.run(req(tmp_path, timeout_s=0.3)))
    assert result.error_kind == "timeout"
    assert [c["signal"] for c in helper_log(log) if "signal" in c] == [2, 15, 9]
    assert "could not signal process group" in caplog.text
    assert time.monotonic() - start >= 0.3 + 2 * GRACE
    # The stand-in refused to signal; stop and reap the CLI ourselves.
    info = read_json(record)
    os.killpg(info["pgid"], 9)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(info["pid"], 0)


def test_healthcheck_goes_through_the_helper(tmp_path):
    switch, log = fake_helper(tmp_path)
    runner, _ = make(tmp_path, switch)
    import asyncio

    asyncio.run(runner.healthcheck())
    execs = [c["exec"] for c in helper_log(log) if "exec" in c]
    assert [runner.config.binary, "--version"] in execs


# -- private files ------------------------------------------------------------------------------


def test_private_file_for_the_agent_group(tmp_path):
    gid = os.getgid()
    directory, path = private_file("secret prompt", "system-append.md", group=gid)
    try:
        assert path.read_text() == "secret prompt"
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750
        assert path.stat().st_gid == gid and directory.stat().st_gid == gid
    finally:
        import shutil

        shutil.rmtree(directory)


def test_private_file_without_group_stays_private():
    directory, path = private_file("x", "f.md")
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    finally:
        import shutil

        shutil.rmtree(directory)


def test_claude_system_prompt_file_uses_the_agent_group(tmp_path):
    switch, _ = fake_helper(tmp_path)
    runner = ClaudeRunner(switch=switch)
    inv = runner.build_invocation(
        RunRequest(prompt="p", cwd=tmp_path, system_append="extra"), "claude", None
    )
    try:
        path = Path(inv.argv[inv.argv.index("--append-system-prompt-file") + 1])
        assert path.stat().st_gid == switch.gid
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        inv.cleanup()


# -- self-test ----------------------------------------------------------------------------------


def test_self_test_without_switch_fails_clearly(tmp_path, monkeypatch):
    from thresy.runner import selftest

    for name in ("inbox", "out"):
        (tmp_path / name).mkdir()
    environ = {"COACH_WORKSPACE_DIR": str(tmp_path), "COACH_DATA_DIR": str(tmp_path / "data")}
    report = selftest.run(RunnerConfig(), environ=environ)
    assert not report.ok
    first = report.checks[0]
    assert first.status == "FAIL" and "COACH_AGENT_USER is not set" in first.detail
    by_name = {c.name: c for c in report.checks}
    assert by_name["workspace exchange"].status == "PASS"
    assert by_name["stops the agent's process group"].status == "PASS"
    assert by_name["stops processes that ignore SIGINT and SIGTERM"].status == "PASS"
    assert "RESULT: FAIL" in report.text()
    assert report.as_dict()["ok"] is False
    # The probe's files are cleaned up.
    assert list((tmp_path / "out").iterdir()) == []
    assert list((tmp_path / "inbox").iterdir()) == []


def test_self_test_reports_a_missing_workspace(tmp_path):
    from thresy.runner import selftest

    report = selftest.run(
        RunnerConfig(), environ={"COACH_WORKSPACE_DIR": str(tmp_path / "missing")}
    )
    assert not report.ok and report.checks[0].name == "workspace"
