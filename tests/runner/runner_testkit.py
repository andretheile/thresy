"""Helpers shared by the runner tests: fixtures, a fake CLI, event collection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from thresy.runner import Final, RunEvent, RunRequest, RunResult
from thresy.runner.cli_base import StreamParser
from thresy.runner.process import ProcessExit

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def exit_info(
    returncode: int | None = 0,
    *,
    timed_out: bool = False,
    cancelled: bool = False,
    stderr: str = "",
) -> ProcessExit:
    return ProcessExit(
        returncode=returncode, timed_out=timed_out, cancelled=cancelled, stderr=stderr
    )


def parse(
    parser: StreamParser, lines: Iterable[str], exit: ProcessExit | None = None
) -> tuple[list[RunEvent], RunResult]:
    events: list[RunEvent] = []
    for line in lines:
        events.extend(parser.feed_line(line))
    return events, parser.finish(exit or exit_info())


def request(tmp_path: Path, prompt: str = "Reply with OK", **kw: Any) -> RunRequest:
    return RunRequest(prompt=prompt, cwd=tmp_path, **kw)


async def collect(handle: Any) -> tuple[list[RunEvent], RunResult]:
    events = [event async for event in handle]
    finals = [e for e in events if isinstance(e, Final)]
    assert len(finals) == 1, f"expected exactly one Final, got {events}"
    assert isinstance(events[-1], Final), "Final must be the last event"
    return events, finals[0].result


def run_collect(handle: Any) -> tuple[list[RunEvent], RunResult]:
    return asyncio.run(collect(handle))


# -- fake CLI -----------------------------------------------------------------

_FAKE_CLI = """\
#!{python}
import json, os, signal, subprocess, sys, time

spec = json.loads(os.environ["FAKE_CLI_SPEC"])
if spec.get("ignore_sigint"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if spec.get("ignore_sigterm"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
record = spec.get("record")
if record:
    data = {{
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "env": {{k: os.environ.get(k) for k in spec.get("env_keys", [])}},
        "env_names": sorted(os.environ),
        "arg_files": {{
            a: {{"text": open(a).read(), "mode": oct(os.stat(a).st_mode & 0o777),
                 "dir_mode": oct(os.stat(os.path.dirname(a)).st_mode & 0o777)}}
            for a in sys.argv[1:] if os.path.isfile(a)
        }},
    }}
    with open(record, "w") as fh:
        json.dump(data, fh)
for step in spec.get("steps", []):
    if "out" in step:
        sys.stdout.write(step["out"] + "\\n")
        sys.stdout.flush()
    elif "out_file" in step:
        with open(step["out_file"]) as fh:
            sys.stdout.write(fh.read())
        sys.stdout.flush()
    elif "out_long" in step:
        sys.stdout.write("x" * step["out_long"] + "\\n")
        sys.stdout.flush()
    elif "err" in step:
        sys.stderr.write(step["err"])
        sys.stderr.flush()
    elif "err_bytes" in step:
        sys.stderr.write("e" * step["err_bytes"])
        sys.stderr.flush()
    elif "sleep" in step:
        time.sleep(step["sleep"])
    elif "spawn" in step:
        # A grandchild in the same process group, like a shell command the agent started.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdout=subprocess.DEVNULL if not step.get("hold_stdout") else None,
            stderr=subprocess.DEVNULL,
        )
        with open(step["spawn"], "w") as fh:
            fh.write(str(child.pid))
sys.exit(spec.get("exit", 0))
"""


def write_fake_cli(directory: Path, name: str = "fake-cli") -> Path:
    path = directory / name
    path.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def spec_env(**spec: Any) -> dict[str, str]:
    return {"FAKE_CLI_SPEC": json.dumps(spec)}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
