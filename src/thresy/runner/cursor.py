"""Cursor CLI ("Cursor Agent") adapter.

NOT YET VERIFIED AGAINST THE REAL CLI. Written from Cursor's official CLI
documentation (cursor.com/docs/cli: headless, output-format, parameters,
authentication) and tested only with fixtures built from its examples.

Command line (working directory = RunRequest.cwd; also passed as --workspace):

    agent -p --output-format stream-json --workspace <cwd>
          [--model <model>] [--resume=<session_id>]
          --force --trust
          [extra args]
          -- <prompt>

The CLI has no option for extra system instructions, so RunRequest.system_append
is prepended to the prompt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from .classify import classify_text, summarize_tool
from .cli_base import CliRunner, Invocation, StreamParser, redact
from .core import Progress, RunEvent, RunnerCapabilities, RunRequest, RunResult, Started
from .process import ProcessExit

log = logging.getLogger(__name__)


class CursorStreamParser(StreamParser):
    """Maps `agent -p --output-format stream-json` events."""

    def __init__(self, runner: str, req: RunRequest, model: str | None) -> None:
        super().__init__(runner, req, model)
        self._result: dict[str, Any] | None = None

    def feed(self, obj: dict[str, Any]) -> list[RunEvent]:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            if isinstance(obj.get("session_id"), str) and obj["session_id"]:
                self.session_id = obj["session_id"]
            if isinstance(obj.get("model"), str) and obj["model"]:
                self.model = obj["model"]
            return [Started(self.session_id)]
        if kind == "assistant":
            if "model_call_id" in obj:
                return []  # duplicate flush emitted with --stream-partial-output
            text = _message_text(obj.get("message"))
            if text.strip():
                self.last_text = text
                return [Progress("text", text)]
            return []
        if kind == "tool_call" and obj.get("subtype") == "started":
            line = _tool_line(obj.get("tool_call"))
            return [Progress("tool", line)] if line else []
        if kind == "result":
            self._result = obj
            if isinstance(obj.get("session_id"), str) and obj["session_id"]:
                self.session_id = obj["session_id"]
            return []
        return []  # user echo, tool_call completed, anything new

    def finish(self, exit: ProcessExit) -> RunResult:
        if exit.cancelled:
            return self.cancelled_result()
        res = self._result
        if res is not None:
            failed = bool(res.get("is_error")) or res.get("subtype") not in (None, "success")
            if not failed:
                # "result" concatenates every assistant segment without separators;
                # the last segment (after the last tool call) is the answer.
                text = self.last_text or str(res.get("result") or "")
                return self.result(ok=True, text=text)
            message = str(res.get("result") or res.get("error") or "Cursor reported an error")
            return self.error(classify_text(message, exit.stderr) or "other", message)
        return self.failure_from_exit(exit)


class CursorRunner(CliRunner):
    name = "cursor"
    capabilities = RunnerCapabilities(
        supports_resume=True, supports_streaming=True, supports_system_append=False
    )
    default_binaries = ("agent", "cursor-agent")
    default_models: Mapping[str, str | None] = {}  # the CLI's default
    default_permission_args = ("--force", "--trust")
    verified = False

    def build_invocation(self, req: RunRequest, binary: str, model: str | None) -> Invocation:
        argv = [binary, "-p", "--output-format", "stream-json", "--workspace", str(req.cwd)]
        if model:
            argv += ["--model", model]
        if req.session_id:
            argv.append(f"--resume={req.session_id}")
        argv += [*self.permission_args, *self.config.extra_args]
        argv += ["--", prepend_system(req.prompt, req.system_append)]
        return Invocation(argv)

    def new_parser(self, req: RunRequest, model: str | None) -> CursorStreamParser:
        return CursorStreamParser(self.name, req, model)

    async def check_auth(
        self, binary: str, env: Mapping[str, str]
    ) -> tuple[bool | None, str, list[str]]:
        exit, out = await self.capture([binary, "status", "--format", "json"], env)
        text = (out or exit.stderr).strip()
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("isAuthenticated", "authenticated", "loggedIn", "isLoggedIn"):
                if isinstance(data.get(key), bool):
                    return data[key], ("authenticated" if data[key] else "not authenticated"), []
        lowered = text.lower()
        if exit.returncode != 0 or "not authenticated" in lowered or "not logged in" in lowered:
            return False, redact(text.splitlines()[0] if text else f"exit {exit.returncode}"), []
        if env.get("CURSOR_API_KEY"):
            return None, "CURSOR_API_KEY is set (validity unknown)", []
        return (
            None,
            "status output not recognised: " + redact(text.splitlines()[0] if text else ""),
            [],
        )


def prepend_system(prompt: str, system_append: str | None) -> str:
    """The contract's fallback for harnesses without a system-prompt option."""
    if not system_append or not system_append.strip():
        return prompt
    return f"{system_append.strip()}\n\n---\n\n{prompt}"


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _tool_line(tool_call: Any) -> str | None:
    if not isinstance(tool_call, dict) or not tool_call:
        return None
    key, body = next(iter(tool_call.items()))
    body = body if isinstance(body, dict) else {}
    if key == "function":
        name = str(body.get("name") or "tool")
        args: Any = body.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"command": args}
        return summarize_tool(name, args if isinstance(args, dict) else {})
    name = key[: -len("ToolCall")] if key.endswith("ToolCall") else key
    name = name[:1].upper() + name[1:]
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    return summarize_tool(name, args)
