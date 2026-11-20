"""``coach runner check --isolation``: does the agent really run apart from the bridge?

It starts a small probe exactly the way an agent run is started (the configured runner's
allow-listed environment, the user switch of ``COACH_AGENT_USER``, the workspace as working
directory) and reports what the probe could and could not do:

* whether it runs as another user, with ``no_new_privs`` set;
* whether it can read the environment of the bridge, of PID 1 or of any other user's process
  through ``/proc``, signal the bridge, read a private file of the bridge, or run the
  user-switch helper itself;
* whether its environment is exactly the allow-list and carries no secret of the bridge;
* where outside the workspace it can write, in particular whether it can write the working
  directory of ``coach`` (which reads a ``.env`` file from there);
* whether the workspace exchange works (the agent reads ``inbox/``, the bridge reads ``out/``);
* whether the bridge can stop the agent's whole process group, once for processes that exit on
  SIGINT and once for processes that ignore SIGINT and SIGTERM.

Run it in the bridge container, as the bridge's user::

    docker compose exec bridge coach runner check --isolation

Standard library only. The probe itself is a short Python program run with ``-I -S`` so it
needs nothing but the interpreter.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import signal
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .claude import ClaudeRunner
from .cli_base import CliRunner
from .config import RunnerConfig
from .isolation import SECURE_MODE_STRIPPED, UserSwitch
from .process import ChildProcess, SpawnError, run_capture
from .registry import get_runner

Status = Literal["PASS", "FAIL", "WARN", "INFO", "SKIP"]

MODEL_CREDENTIALS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "CURSOR_API_KEY",
)
COERCED_LOCALE = "LC_CTYPE"
"""Python sets it in its own environment when no locale is configured (PEP 538)."""
PLATFORM_NAMES = frozenset({"__CF_USER_TEXT_ENCODING"})
"""Variables the operating system adds to every process (macOS), ignored by the comparison."""
MIN_SECRET_LEN = 8
"""Values shorter than this are not compared (they are not secrets, and would match by
accident)."""

_PROBE = r"""
import errno, hashlib, json, os, sys

args = json.loads(sys.argv[1])
me = os.getuid()
r = {"uid": me, "euid": os.geteuid(), "gid": os.getgid(), "groups": sorted(os.getgroups()),
     "pid": os.getpid(), "pgid": os.getpgid(0), "cwd": os.getcwd()}
old = os.umask(0o022)
os.umask(old)
r["umask"] = old
r["env_names"] = sorted(os.environ)
r["env_hashes"] = sorted({hashlib.sha256(v.encode()).hexdigest()
                          for v in os.environ.values() if len(v) >= args["min_len"]})
r["no_new_privs"] = None
try:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("NoNewPrivs:"):
                r["no_new_privs"] = line.split()[1] == "1"
except OSError:
    pass

def read_environ(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            fh.read(1)
        return "readable"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return "error: %s" % (exc.strerror or exc)

r["environ_parent"] = read_environ(args["parent_pid"])
r["environ_pid1"] = read_environ(1)
others = {"tried": 0, "readable": []}
if os.path.isdir("/proc"):
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            owner = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            continue
        if owner == me:
            continue
        outcome = read_environ(pid)
        if outcome == "missing":
            continue
        others["tried"] += 1
        if outcome == "readable":
            others["readable"].append(pid)
r["environ_others"] = others

def try_signal(pid):
    try:
        os.kill(pid, 0)
        return "allowed"
    except PermissionError:
        return "denied"
    except ProcessLookupError:
        return "gone"

r["signal_parent"] = try_signal(args["parent_pid"])
r["signal_pid1"] = try_signal(1)

def try_read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except PermissionError:
        return None
    except OSError as exc:
        return "error: %s" % (exc.strerror or exc)

secret = try_read(args["private_file"])
r["private_file"] = "denied" if secret is None else (
    "readable" if secret == args["private_token"] else secret)
r["helper_executable"] = bool(args["helper"]) and os.access(args["helper"], os.X_OK)

def try_write(directory):
    if not os.path.isdir(directory):
        return "missing"
    path = os.path.join(directory, ".thresy-isolation-probe-%d" % os.getpid())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.unlink(path)
        return "writable"
    except PermissionError:
        return "denied"
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return "read-only"
        return "error: %s" % (exc.strerror or exc)

r["writes"] = {d: try_write(d) for d in args["write_paths"]}
if args.get("inbox_file"):
    got = try_read(args["inbox_file"])
    r["inbox_read"] = "ok" if got == args["exchange_token"] else ("denied" if got is None else got)
if args.get("out_file"):
    try:
        with open(args["out_file"], "x") as fh:
            fh.write(args["exchange_token"])
        r["out_write"] = "ok"
    except OSError as exc:
        r["out_write"] = "error: %s" % (exc.strerror or exc)
cred = args.get("credentials_file")
if cred:
    try:
        st = os.stat(cred)
        names = []
        with open(cred) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name = line.split("=", 1)[0].strip()
                if name.startswith("export "):
                    name = name[7:].strip()
                names.append(name)
        r["credentials"] = {"present": True, "mode": st.st_mode & 0o777, "names": names}
    except FileNotFoundError:
        r["credentials"] = {"present": False}
    except OSError as exc:
        r["credentials"] = {"present": None, "error": exc.strerror or str(exc)}
print(json.dumps(r), flush=True)
"""

_LINGER = r"""
import json, signal, subprocess, sys, time

stubborn = sys.argv[1] == "stubborn"
if stubborn:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
code = ("import signal, time\n"
        + ("signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
           "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if stubborn else "")
        + "time.sleep(600)\n")
child = subprocess.Popen([sys.executable, "-I", "-S", "-c", code])
print(json.dumps({"pid": child.pid}), flush=True)
while True:
    time.sleep(1)
"""


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


@dataclass
class IsolationReport:
    user: str | None
    helper: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "FAIL" for c in self.checks)

    def add(self, status: Status, name: str, detail: str) -> None:
        self.checks.append(Check(status, name, detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "agent_user": self.user,
            "helper": self.helper,
            "checks": [
                {"status": c.status, "name": c.name, "detail": c.detail} for c in self.checks
            ],
        }

    def text(self) -> str:
        head = (
            f"Agent isolation self-test (COACH_AGENT_USER={self.user}, helper {self.helper})"
            if self.user
            else "Agent isolation self-test (COACH_AGENT_USER is not set)"
        )
        lines = [head]
        lines += [f"{c.status:<4}  {c.name}: {c.detail}" for c in self.checks]
        failed = sum(c.status == "FAIL" for c in self.checks)
        if failed:
            lines.append(f"RESULT: FAIL ({failed} check(s) failed). The agent is NOT isolated.")
        else:
            lines.append(
                "RESULT: PASS. The agent runs as its own user and cannot reach the bridge's "
                "environment, files or processes."
            )
        return "\n".join(lines)


def _adapter(config: RunnerConfig, name: str | None) -> CliRunner:
    runner = get_runner(name, config)
    if isinstance(runner, CliRunner):
        return runner
    return ClaudeRunner(config.claude, switch=config.user_switch)  # the fake runner


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_states(pgid: int) -> list[str]:
    """Process states ("S", "Z", ...) of the members of process group ``pgid``."""
    states = []
    with contextlib.suppress(OSError):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            fields = stat[stat.rfind(")") + 2 :].split()
            if len(fields) > 2 and fields[2] == str(pgid):
                states.append(fields[0])
    return states


async def _stop_test(
    switch: UserSwitch | None, env: Mapping[str, str], cwd: Path, mode: str, grace_s: float
) -> tuple[bool, float, int | None, str]:
    """Start a probe that leaves a grandchild in its group, stop the group like a timed-out
    run, and return (group gone, seconds to stop, return code, note)."""
    argv = [sys.executable, "-I", "-S", "-c", _LINGER, mode]
    child = ChildProcess(argv, cwd=cwd, env=env, grace_s=grace_s, switch=switch)
    loop = asyncio.get_running_loop()
    started = False
    async with child:
        lines = child.lines(timeout_s=20)
        async with contextlib.aclosing(lines):
            async for line in lines:
                with contextlib.suppress(ValueError):
                    if "pid" in json.loads(line):
                        started = True
                        break
        begin = loop.time()
    elapsed = loop.time() - begin
    pgid = child.pid
    assert pgid is not None and child.exit is not None
    gone = not _group_alive(pgid)
    note = ""
    if not started:
        note = f"the probe did not start: {child.exit.stderr.strip()[-300:]}"
        return False, elapsed, child.exit.returncode, note
    if not gone:
        states = _group_states(pgid)
        if states and all(s == "Z" for s in states):
            note = (
                "only zombie processes are left: run the container with an init process "
                "(init: true in compose.yml)"
            )
    return gone, elapsed, child.exit.returncode, note


def _signal_name(returncode: int | None) -> str:
    if returncode is None:
        return "not reaped"
    if returncode < 0:
        with contextlib.suppress(ValueError):
            return signal.Signals(-returncode).name
        return f"signal {-returncode}"
    return f"exit {returncode}"


async def run_self_test(
    config: RunnerConfig,
    *,
    environ: Mapping[str, str] | None = None,
    runner: str | None = None,
    grace_s: float = 0.5,
) -> IsolationReport:
    """Run the probe and return the report. ``environ`` is the bridge's configuration (default
    ``os.environ``); the probe's environment is built from ``os.environ`` like a real run."""
    env_cfg = os.environ if environ is None else environ
    switch = config.user_switch
    report = IsolationReport(
        user=switch.user if switch else None, helper=switch.helper if switch else None
    )
    workspace = Path(env_cfg.get("COACH_WORKSPACE_DIR") or ".").expanduser().resolve()
    data_dir = Path(env_cfg.get("COACH_DATA_DIR") or "").expanduser()
    if not workspace.is_dir():
        report.add("FAIL", "workspace", f"{workspace} does not exist (COACH_WORKSPACE_DIR)")
        return report

    adapter = _adapter(config, runner)
    base_env = adapter.child_env(None)
    expected = switch.environment(base_env) if switch else dict(base_env)

    # A private file of the bridge and the two exchange files.
    fd, private_name = tempfile.mkstemp(prefix="thresy-isolation-")
    private_token = secrets.token_hex(16)
    with os.fdopen(fd, "w") as fh:
        fh.write(private_token)
    exchange_token = secrets.token_hex(16)
    tag = f".thresy-isolation-{os.getpid()}-{secrets.token_hex(4)}"
    inbox, out = workspace / "inbox", workspace / "out"
    inbox_file = inbox / tag if inbox.is_dir() else None
    out_file = out / tag if out.is_dir() else None
    if inbox_file is not None:
        inbox_file.write_text(exchange_token)

    agent_home = Path(switch.home) if switch else Path(expected.get("HOME") or "~").expanduser()
    bridge_home = Path(os.environ.get("HOME") or "~").expanduser()
    app_root = Path(__file__).resolve().parents[3]
    must_write = [str(workspace), str(agent_home)]
    must_not = [
        str(bridge_home),
        str(app_root),
        sys.prefix,
        "/usr/local/bin",
        "/etc",
        "/",
    ]
    if str(data_dir) not in ("", "."):
        must_not.insert(1, str(data_dir.resolve()))
    if switch is None:
        # Without a switch the agent is this user: its home is the bridge's home.
        must_not = [p for p in must_not if p != str(bridge_home)]
    info_write = [tempfile.gettempdir()]
    service_cwd = str(Path.cwd())
    args = {
        "parent_pid": os.getpid(),
        "min_len": MIN_SECRET_LEN,
        "private_file": private_name,
        "private_token": private_token,
        "helper": switch.helper if switch else "",
        "write_paths": list(dict.fromkeys([*must_write, *must_not, *info_write, service_cwd])),
        "inbox_file": str(inbox_file) if inbox_file else None,
        "out_file": str(out_file) if out_file else None,
        "exchange_token": exchange_token,
        "credentials_file": str(switch.credentials_file) if switch else None,
    }
    argv = [sys.executable, "-I", "-S", "-c", _PROBE, json.dumps(args)]
    try:
        exit, stdout = await run_capture(
            argv, env=base_env, cwd=workspace, timeout_s=30, switch=switch
        )
    except SpawnError as exc:
        report.add("FAIL", "start", f"the probe could not be started: {exc}")
        _cleanup(private_name, inbox_file, out_file)
        return report
    try:
        probe = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        detail = exit.stderr.strip()[-500:] or f"exit {exit.returncode}, no output"
        report.add("FAIL", "start", f"the probe failed: {detail}")
        _cleanup(private_name, inbox_file, out_file)
        return report

    try:
        _judge(report, probe, switch, expected, env_cfg, args, must_write, must_not, info_write)
        _judge_cwd(report, probe, service_cwd)
        _judge_exchange(report, probe, inbox_file, out_file, exchange_token, workspace)
        _judge_credentials(report, probe, switch)
    finally:
        _cleanup(private_name, inbox_file, out_file)

    for mode, label in (
        ("cooperative", "stops the agent's process group"),
        ("stubborn", "stops processes that ignore SIGINT and SIGTERM"),
    ):
        try:
            gone, elapsed, rc, note = await _stop_test(switch, base_env, workspace, mode, grace_s)
        except SpawnError as exc:
            report.add("FAIL", label, f"could not start the probe: {exc}")
            continue
        if gone:
            report.add(
                "PASS",
                label,
                f"the probe and its child were gone {elapsed:.1f} s after the stop began "
                f"(the probe ended by {_signal_name(rc)})",
            )
        else:
            report.add("FAIL", label, f"processes are left after {elapsed:.1f} s. {note}".strip())
    return report


def _judge(
    report: IsolationReport,
    probe: dict[str, Any],
    switch: UserSwitch | None,
    expected: Mapping[str, str],
    env_cfg: Mapping[str, str],
    args: dict[str, Any],
    must_write: list[str],
    must_not: list[str],
    info_write: list[str],
) -> None:
    me = os.getuid()
    uid = probe["uid"]
    if switch is None:
        report.add(
            "FAIL",
            "runs as another user",
            f"no: COACH_AGENT_USER is not set, so the agent runs as uid {uid}, the bridge's own "
            "user, and can read everything the bridge can",
        )
    elif uid == switch.uid and probe["euid"] == switch.uid and uid != me:
        report.add(
            "PASS",
            "runs as another user",
            f"uid {uid} ({switch.user}), groups {probe['groups']}; the bridge is uid {me}",
        )
    else:
        report.add(
            "FAIL",
            "runs as another user",
            f"the probe ran as uid {uid} (euid {probe['euid']}); expected {switch.uid}",
        )

    nnp = probe.get("no_new_privs")
    if nnp is None:
        report.add("SKIP", "no_new_privs", "not reported (no /proc/self/status)")
    elif nnp:
        report.add("PASS", "no_new_privs", "set: setuid programs cannot raise the agent's rights")
    else:
        report.add(
            "FAIL" if switch else "WARN",
            "no_new_privs",
            "not set: a setuid program could raise the agent's rights",
        )

    parent, pid1 = probe["environ_parent"], probe["environ_pid1"]
    others = probe["environ_others"]
    if parent == "missing" and pid1 == "missing":
        report.add("SKIP", "reads other processes' environment", "no /proc (not Linux)")
    else:
        leaks = []
        if parent == "readable":
            leaks.append(f"the bridge (pid {args['parent_pid']})")
        if pid1 == "readable":
            leaks.append("PID 1")
        if others["readable"]:
            leaks.append(f"other users' processes {others['readable'][:10]}")
        if leaks:
            report.add("FAIL", "reads other processes' environment", "yes: " + ", ".join(leaks))
        else:
            report.add(
                "PASS",
                "reads other processes' environment",
                f"no: the bridge {parent}, PID 1 {pid1}, {others['tried']} process(es) of "
                "other users tried, all denied",
            )

    sig = probe["signal_parent"]
    report.add(
        "PASS" if sig == "denied" else "FAIL",
        "signals the bridge",
        "no (not permitted)" if sig == "denied" else f"yes ({sig})",
    )

    private = probe["private_file"]
    report.add(
        "PASS" if private == "denied" else "FAIL",
        "reads a private file of the bridge",
        "no (permission denied)" if private == "denied" else f"yes ({private})",
    )

    if switch is not None:
        report.add(
            "FAIL" if probe["helper_executable"] else "PASS",
            "runs the user-switch helper",
            "yes: check its mode (4750 root:coach)"
            if probe["helper_executable"]
            else "no (not executable for the agent)",
        )

    names = set(probe["env_names"]) - PLATFORM_NAMES
    cred = probe.get("credentials") or {}
    cred_names = set(cred.get("names") or [])
    unexpected = sorted(names - set(expected) - cred_names)
    if COERCED_LOCALE in unexpected:
        unexpected.remove(COERCED_LOCALE)  # set by the probe's own interpreter (PEP 538)
    missing = sorted(set(expected) - names)
    stripped = [n for n in missing if switch is not None and n in SECURE_MODE_STRIPPED]
    lost = [n for n in missing if n not in stripped]
    if unexpected or lost:
        parts = []
        if unexpected:
            parts.append(f"unexpected {unexpected}")
        if lost:
            parts.append(f"missing {lost}")
        report.add("FAIL", "environment is the allow-list", "; ".join(parts))
    else:
        detail = f"{len(names)} variables: {', '.join(sorted(names))}"
        if stripped:
            detail += f" (removed by the C library for the setuid helper: {', '.join(stripped)})"
        report.add("PASS", "environment is the allow-list", detail)

    hashes = set(probe["env_hashes"])
    checked, leaked = 0, []
    for key, value in env_cfg.items():
        if (
            key in expected
            or key in cred_names
            or key in PLATFORM_NAMES
            or len(value) < MIN_SECRET_LEN
        ):
            continue
        checked += 1
        if _sha(value) in hashes:
            leaked.append(key)
    if leaked:
        report.add(
            "FAIL",
            "secrets of the bridge in the agent's environment",
            f"the values of {sorted(leaked)} reached the agent",
        )
    else:
        report.add(
            "PASS",
            "secrets of the bridge in the agent's environment",
            f"none: {checked} value(s) of the bridge's environment that are not on the "
            "allow-list were compared",
        )
    in_bridge = [n for n in MODEL_CREDENTIALS if env_cfg.get(n)]
    if in_bridge and switch is not None:
        report.add(
            "WARN",
            "model credential in the bridge's environment",
            f"{', '.join(in_bridge)} is set for the bridge; the agent's credential file keeps it "
            "out of the bridge (docs/deploy.md)",
        )

    writes = probe["writes"]
    bad = [p for p in must_not if writes.get(p) == "writable"]
    blocked = [f"{p} ({writes.get(p)})" for p in must_write if writes.get(p) != "writable"]
    if bad:
        report.add("FAIL", "writes outside the workspace", f"can write to {bad}")
    else:
        denied = ", ".join(f"{p} ({writes.get(p)})" for p in must_not)
        tmp = ", ".join(f"{p} ({writes.get(p)})" for p in info_write)
        report.add(
            "PASS",
            "writes outside the workspace",
            f"only its home and {tmp}; not {denied}",
        )
    if blocked:
        report.add(
            "FAIL",
            "writes to the workspace and its home",
            f"cannot write to {blocked}; the agent needs both",
        )
    if switch is not None and probe["umask"] != switch.umask:
        report.add(
            "WARN", "umask", f"{probe['umask']:03o}, expected {switch.umask:03o} for the agent"
        )


def _judge_cwd(report: IsolationReport, probe: dict[str, Any], cwd: str) -> None:
    """The bridge and `coach` read a .env file from their working directory, so the agent
    must not be able to write there (the image runs everything from /app)."""
    outcome = probe["writes"].get(cwd)
    name = "writes the service's working directory"
    if outcome == "writable":
        report.add(
            "FAIL",
            name,
            f"yes: {cwd} is writable for the agent, and `coach` reads a .env file from its "
            "working directory, so the agent could configure the bridge; run the services from "
            "a directory the agent cannot write (the image uses /app)",
        )
    else:
        report.add("PASS", name, f"no: {cwd} ({outcome}), where `coach` looks for a .env file")


def _judge_exchange(
    report: IsolationReport,
    probe: dict[str, Any],
    inbox_file: Path | None,
    out_file: Path | None,
    token: str,
    workspace: Path,
) -> None:
    if inbox_file is None or out_file is None:
        report.add(
            "FAIL",
            "workspace exchange",
            f"{workspace}/inbox or out is missing (run `coach workspace init`)",
        )
        return
    problems = []
    if probe.get("inbox_read") != "ok":
        problems.append(f"the agent cannot read inbox/ ({probe.get('inbox_read')})")
    if probe.get("out_write") != "ok":
        problems.append(f"the agent cannot write out/ ({probe.get('out_write')})")
    else:
        try:
            if out_file.read_text() != token:
                problems.append("the bridge read something else from out/")
        except OSError as exc:
            problems.append(f"the bridge cannot read out/ ({exc.strerror or exc})")
    if problems:
        report.add("FAIL", "workspace exchange", "; ".join(problems))
    else:
        report.add(
            "PASS",
            "workspace exchange",
            "the bridge writes inbox/ and the agent reads it; the agent writes out/ and the "
            "bridge reads it",
        )


def _judge_credentials(
    report: IsolationReport, probe: dict[str, Any], switch: UserSwitch | None
) -> None:
    if switch is None:
        return
    path = switch.credentials_file
    cred = probe.get("credentials") or {}
    try:
        bridge_can_read = os.access(path, os.R_OK)
    except OSError:
        bridge_can_read = False
    if bridge_can_read:
        report.add(
            "FAIL",
            "agent credential file",
            f"the bridge's user can read {path}; it must belong to {switch.user}, mode 600",
        )
    elif cred.get("present"):
        mode = int(cred.get("mode") or 0)
        names = ", ".join(cred.get("names") or []) or "no variables"
        status: Status = "WARN" if mode & 0o077 else "INFO"
        report.add(
            status,
            "agent credential file",
            f"{path}: mode {mode:03o}, not readable by the bridge; sets {names}",
        )
    elif cred.get("present") is False:
        report.add(
            "INFO",
            "agent credential file",
            f"{path} does not exist; the model credential must then come from the agent's "
            "own login or the bridge's environment (docs/deploy.md)",
        )
    else:
        error = cred.get("error")
        report.add("WARN", "agent credential file", f"{path}: the agent cannot read it ({error})")


def _cleanup(private_name: str, *paths: Path | None) -> None:
    with contextlib.suppress(OSError):
        Path(private_name).unlink()
    for path in paths:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


def run(
    config: RunnerConfig, *, environ: Mapping[str, str] | None = None, runner: str | None = None
) -> IsolationReport:
    """Synchronous wrapper for the command line."""
    return asyncio.run(run_self_test(config, environ=environ, runner=runner))
