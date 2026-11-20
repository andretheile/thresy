"""Standard-library command line, `python -m thresy.runner check|run ...`, plus the
helpers that `coach runner ...` (thresy.runner.cli, typer) shares with it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config import RUNNER_NAMES, RunnerConfig
from .core import Final, Health, Progress, RunEvent, RunRequest, RunResult, Started
from .handle import RunHandle
from .registry import get_runner

# -- shared helpers -------------------------------------------------------------


def runner_names(name: str | None, all_runners: bool, config: RunnerConfig) -> list[str]:
    if all_runners:
        return list(RUNNER_NAMES)
    chosen = (name or config.runner).strip().lower()
    if chosen not in RUNNER_NAMES:
        raise ValueError(f"unknown runner {chosen!r}; expected one of {', '.join(RUNNER_NAMES)}")
    return [chosen]


async def check(names: list[str], config: RunnerConfig) -> list[Health]:
    return [await get_runner(name, config).healthcheck() for name in names]


def health_dict(health: Health) -> dict[str, Any]:
    return dict(dataclasses.asdict(health), ok=health.ok)


def health_text(health: Health) -> str:
    lines = [f"{'OK  ' if health.ok else 'FAIL'} {health.runner}: {health.detail}"]
    if health.binary_path:
        lines.append(f"     binary: {health.binary_path}")
    lines.extend(f"     warning: {w}" for w in health.warnings)
    return "\n".join(lines)


def event_json(event: RunEvent) -> str:
    payload = dataclasses.asdict(event)
    payload["event"] = type(event).__name__.lower()
    return json.dumps(payload, ensure_ascii=False, default=str)


def event_text(event: RunEvent) -> str:
    if isinstance(event, Started):
        return f"[started] session {event.session_id}"
    if isinstance(event, Progress):
        return f"[{event.kind}] {event.text}"
    assert isinstance(event, Final)
    r = event.result
    status = "ok" if r.ok else f"FAILED ({r.error_kind}): {r.error_detail}"
    cost = f", cost ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
    line = (
        f"[final] {status}; runner {r.runner}, model {r.model}, "
        f"session {r.session_id}, {r.duration_s:.1f}s{cost}"
    )
    return f"{line}\n{r.text}" if r.text else line


def build_request(
    *,
    prompt: str,
    cwd: Path,
    resume: str | None,
    tier: str,
    timeout: float,
    system_append: str | None,
) -> RunRequest:
    if prompt == "-":
        prompt = sys.stdin.read()
    return RunRequest(
        prompt=prompt,
        cwd=cwd.expanduser().resolve(),
        session_id=resume,
        model_tier="fast" if tier == "fast" else "smart",
        timeout_s=timeout,
        system_append=system_append,
    )


async def run_and_print(
    name: str | None,
    config: RunnerConfig,
    req: RunRequest,
    *,
    as_json: bool,
    echo: Callable[[str], None],
) -> RunResult:
    """Run `req`, print every event as it arrives; the first Ctrl+C cancels the run."""
    runner = get_runner(name, config)
    handle = runner.run(req)
    assert isinstance(handle, RunHandle)
    loop = asyncio.get_running_loop()

    def interrupt() -> None:
        print("interrupted: stopping the run (Ctrl+C again to abort)", file=sys.stderr)
        handle.cancel("interrupted by user")
        loop.remove_signal_handler(signal.SIGINT)

    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGINT, interrupt)
    try:
        async with handle:
            async for event in handle:
                echo(event_json(event) if as_json else event_text(event))
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(signal.SIGINT)
    return await handle.result()


# -- argparse entry point ---------------------------------------------------------


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m thresy.runner")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="check that a runner's CLI is installed and logged in")
    chk.add_argument("--runner", help="claude, codex, cursor or fake (default: COACH_RUNNER)")
    chk.add_argument("--all", action="store_true", help="check every runner")
    chk.add_argument(
        "--isolation",
        action="store_true",
        help="instead: check that the agent runs as its own user, apart from the bridge",
    )
    chk.add_argument("--json", action="store_true", help="print JSON")

    run = sub.add_parser("run", help="run a prompt and print events as they arrive")
    run.add_argument("--runner", help="claude, codex, cursor or fake (default: COACH_RUNNER)")
    run.add_argument("--cwd", required=True, type=Path, help="working directory of the agent")
    run.add_argument("--resume", metavar="SESSION_ID", help="harness session id to resume")
    run.add_argument("--tier", choices=("fast", "smart"), default="smart")
    run.add_argument("--timeout", type=float, default=900.0, help="seconds (default 900)")
    run.add_argument("--system-append", help="extra system instructions")
    run.add_argument("--json", action="store_true", help="print one JSON object per event")
    run.add_argument("prompt", help='the prompt; "-" reads it from stdin')

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = RunnerConfig.from_env(environ)
        if args.command == "check" and args.isolation:
            from . import selftest

            runner_names(args.runner, False, config)
            report = selftest.run(config, environ=environ, runner=args.runner)
            print(json.dumps(report.as_dict(), indent=2) if args.json else report.text())
            return 0 if report.ok else 1
        if args.command == "check":
            names = runner_names(args.runner, args.all, config)
            healths = asyncio.run(check(names, config))
            if args.json:
                data = [health_dict(h) for h in healths]
                print(json.dumps(data if args.all else data[0], indent=2))
            else:
                print("\n".join(health_text(h) for h in healths))
            return 0 if all(h.ok for h in healths) else 1
        runner_names(args.runner, False, config)  # validate the name early
        req = build_request(
            prompt=args.prompt,
            cwd=args.cwd,
            resume=args.resume,
            tier=args.tier,
            timeout=args.timeout,
            system_append=args.system_append,
        )
        result = asyncio.run(
            run_and_print(args.runner, config, req, as_json=args.json, echo=_print)
        )
        return 0 if result.ok else 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _print(line: str) -> None:
    print(line, flush=True)
