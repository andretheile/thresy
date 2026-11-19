"""Command-line entry: `python -m thresy.runner check|run ...`.

The project's typer CLI (`coach runner check`) can wrap these functions later.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import signal
import sys
from pathlib import Path

from .config import RUNNER_NAMES, RunnerConfig
from .core import Final, Health, Progress, RunEvent, RunRequest, Started
from .registry import get_runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m thresy.runner")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check that a runner's CLI is installed and logged in")
    check.add_argument("--runner", choices=RUNNER_NAMES, help="default: COACH_RUNNER")
    check.add_argument("--all", action="store_true", help="check every runner")
    check.add_argument("--json", action="store_true", help="print JSON")

    run = sub.add_parser("run", help="run a prompt and print events as they arrive")
    run.add_argument("--runner", choices=RUNNER_NAMES, help="default: COACH_RUNNER")
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
        config = RunnerConfig.from_env()
        if args.command == "check":
            return asyncio.run(_check(args, config))
        return asyncio.run(_run(args, config))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


async def _check(args: argparse.Namespace, config: RunnerConfig) -> int:
    names = list(RUNNER_NAMES) if args.all else [args.runner or config.runner]
    results: list[Health] = []
    for name in names:
        results.append(await get_runner(name, config).healthcheck())
    if args.json:
        payload = [dict(dataclasses.asdict(h), ok=h.ok) for h in results]
        print(json.dumps(payload if args.all else payload[0], indent=2))
    else:
        for h in results:
            print(f"{'OK  ' if h.ok else 'FAIL'} {h.runner}: {h.detail}")
            if h.binary_path:
                print(f"     binary: {h.binary_path}")
            for warning in h.warnings:
                print(f"     warning: {warning}")
    return 0 if all(h.ok for h in results) else 1


async def _run(args: argparse.Namespace, config: RunnerConfig) -> int:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    runner = get_runner(args.runner, config)
    req = RunRequest(
        prompt=prompt,
        cwd=args.cwd.expanduser().resolve(),
        session_id=args.resume,
        model_tier=args.tier,
        timeout_s=args.timeout,
        system_append=args.system_append,
    )
    handle = runner.run(req)
    loop = asyncio.get_running_loop()

    def interrupt() -> None:
        print("interrupted: stopping the run (Ctrl+C again to abort)", file=sys.stderr)
        handle.cancel("interrupted by user")  # type: ignore[attr-defined]
        loop.remove_signal_handler(signal.SIGINT)

    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGINT, interrupt)
    ok = False
    async with handle:  # type: ignore[attr-defined]
        async for event in handle:
            _print_event(event, as_json=args.json)
            if isinstance(event, Final):
                ok = event.result.ok
    return 0 if ok else 1


def _print_event(event: RunEvent, *, as_json: bool) -> None:
    if as_json:
        payload = dataclasses.asdict(event)
        payload["event"] = type(event).__name__.lower()
        print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
        return
    if isinstance(event, Started):
        print(f"[started] session {event.session_id}", flush=True)
    elif isinstance(event, Progress):
        print(f"[{event.kind}] {event.text}", flush=True)
    elif isinstance(event, Final):
        r = event.result
        status = "ok" if r.ok else f"FAILED ({r.error_kind}): {r.error_detail}"
        cost = f", cost ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
        print(
            f"[final] {status}; runner {r.runner}, model {r.model}, "
            f"session {r.session_id}, {r.duration_s:.1f}s{cost}",
            flush=True,
        )
        if r.text:
            print(r.text, flush=True)
