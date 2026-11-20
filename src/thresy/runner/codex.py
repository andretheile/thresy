"""OpenAI Codex CLI adapter.

NOT YET VERIFIED AGAINST THE REAL CLI. Written from the official
documentation (developers.openai.com/codex: non-interactive mode, CLI
reference) and the JSONL event definitions in the openai/codex repository
(codex-rs/exec/src/exec_events.rs); tested only with fixtures built from them.

Command line (prompt on stdin via "-", working directory = RunRequest.cwd):

    codex exec --json --skip-git-repo-check --cd <cwd> -c features.memories=false
          [-c model_reasoning_effort="<level>"] [--model <model>]
          --dangerously-bypass-approvals-and-sandbox
          [extra args]
          [resume <session_id>] -

All exec options come before the `resume` subcommand because some of them
(--cd, --sandbox) are not global options of `codex exec`.

RunRequest.effort becomes the `model_reasoning_effort` setting (documented values
low, medium, high, xhigh, max, ultra; "available levels depend on the model").
What Codex does with a level the model lacks is not documented, so the shared
runner retries a run that fails before any output once without it.

RunRequest.system_append is prepended to the stdin prompt. Codex's
`developer_instructions` setting can only be given as a `-c` argument (visible
to every user on the host via `ps`), and its file setting
`model_instructions_file` replaces the built-in instructions instead of adding
to them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from .classify import classify_text, summarize_tool, truncate
from .cli_base import (
    CliRunner,
    Invocation,
    StreamParser,
    prepend_system,
    redact,
    usage_dict,
)
from .core import Progress, RunEvent, RunnerCapabilities, RunRequest, RunResult, Started
from .process import ProcessExit

log = logging.getLogger(__name__)

_USAGE_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_input_tokens",
    "reasoning_tokens": "reasoning_output_tokens",
}
_SHELL_WRAPPER = re.compile(r"^(?:\S*/)?(?:ba|z)?sh\s+-l?c\s+(?P<cmd>.+)$", re.DOTALL)


class CodexStreamParser(StreamParser):
    """Maps `codex exec --json` events (thread.*, turn.*, item.*, error)."""

    def __init__(self, runner: str, req: RunRequest, model: str | None) -> None:
        super().__init__(runner, req, model)
        self._completed = False
        self._failed: str | None = None
        self._errors: list[str] = []
        self._usage: dict[str, int] = {}
        self._announced: set[str] = set()

    def feed(self, obj: dict[str, Any]) -> list[RunEvent]:
        kind = obj.get("type")
        if kind == "thread.started":
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.session_id = thread_id
            return [Started(self.session_id)]
        if kind in ("item.started", "item.updated", "item.completed"):
            item = obj.get("item")
            return self._item(kind, item) if isinstance(item, dict) else []
        if kind == "turn.completed":
            self._completed = True
            self.terminal_seen = True
            for key, value in (obj.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self._usage[key] = self._usage.get(key, 0) + value
            return []
        if kind == "turn.failed":
            error = obj.get("error") or {}
            self._failed = str(error.get("message") or "turn failed")
            self.terminal_seen = True
            return []
        if kind == "error":
            message = obj.get("message")
            if message:
                self._errors.append(str(message))
            return []
        return []  # turn.started and anything new

    def _item(self, kind: str, item: dict[str, Any]) -> list[RunEvent]:
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            text = item.get("text") or ""
            if kind == "item.completed" and text.strip():
                self.last_text = text
                return [Progress("text", text)]
            return []
        if item_type == "error":
            if kind == "item.completed" and item.get("message"):
                self._errors.append(str(item["message"]))
            return []
        line = self._tool_line(item_type, item)
        if line is None:
            return []
        # One line per tool call: on start, or on completion if no start was seen.
        if kind == "item.updated" or (item_id and item_id in self._announced):
            return []
        if item_id:
            self._announced.add(item_id)
        return [Progress("tool", line)]

    @staticmethod
    def _tool_line(item_type: Any, item: dict[str, Any]) -> str | None:
        if item_type == "command_execution":
            return summarize_tool(
                "shell", {"command": _unwrap_shell(str(item.get("command") or ""))}
            )
        if item_type == "file_change":
            paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
            return "edit: " + truncate(", ".join(p for p in paths if p) or "files")
        if item_type == "mcp_tool_call":
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            return summarize_tool(f"{item.get('server', 'mcp')}/{item.get('tool', 'tool')}", args)
        if item_type == "web_search":
            return summarize_tool("web search", {"query": str(item.get("query") or "")})
        if item_type == "collab_tool_call":
            return summarize_tool("agent", {"command": str(item.get("tool") or "")})
        return None  # reasoning, todo_list, unknown

    def finish(self, exit: ProcessExit) -> RunResult:
        usage = usage_dict(self._usage, _USAGE_KEYS)
        if exit.cancelled:
            return self.cancelled_result(usage=usage)
        if self._failed is not None:
            kind = classify_text(self._failed, *self._errors) or "other"
            return self.error(kind, self._failed, usage=usage)
        if self._completed:
            if exit.returncode not in (0, None):
                log.warning("codex: turn completed but exit code was %s", exit.returncode)
            return self.result(ok=True, usage=usage)
        last_error = self._errors[-1] if self._errors else None
        return self.failure_from_exit(exit, last_error, usage=usage)


class CodexRunner(CliRunner):
    name = "codex"
    capabilities = RunnerCapabilities(
        supports_resume=True,
        supports_streaming=True,
        supports_system_append=False,
        supports_effort=True,
    )
    default_binaries = ("codex",)
    default_models: Mapping[str, str | None] = {}  # the CLI's configured default
    default_permission_args = ("--dangerously-bypass-approvals-and-sandbox",)
    # Documented: CODEX_HOME, CODEX_API_KEY, CODEX_CA_CERTIFICATE, OPENAI_API_KEY.
    env_names = frozenset({"OPENAI_API_KEY"})
    env_prefixes = ("CODEX_",)
    verified = False

    def build_invocation(self, req: RunRequest, binary: str, model: str | None) -> Invocation:
        argv = [binary, "exec", "--json", "--skip-git-repo-check", "--cd", str(req.cwd)]
        # Memories are off by default, but the agent's config.toml could turn them on; memory
        # is the workspace's memory/ files. A -c override outranks every config file.
        argv += ["-c", "features.memories=false"]
        if req.effort:  # -c values are TOML: a quoted string
            argv += ["-c", f'model_reasoning_effort="{req.effort}"']
        if model:
            argv += ["--model", model]
        argv += [*self.permission_args, *self.config.extra_args]
        if req.session_id:
            argv += ["resume", req.session_id]
        argv.append("-")
        prompt = prepend_system(req.prompt, req.system_append)
        return Invocation(argv, stdin=prompt.encode("utf-8"))

    def new_parser(self, req: RunRequest, model: str | None) -> CodexStreamParser:
        return CodexStreamParser(self.name, req, model)

    async def check_auth(
        self, binary: str, env: Mapping[str, str]
    ) -> tuple[bool | None, str, list[str]]:
        warnings = []
        if env.get("CODEX_API_KEY") or env.get("OPENAI_API_KEY"):
            warnings.append("an API key variable is set; runs may be billed to the API key")
        exit, out = await self.capture([binary, "login", "status"], env)
        message = redact(
            (out or exit.stderr).strip().splitlines()[0] if (out or exit.stderr).strip() else ""
        )
        if exit.returncode == 0:
            return True, message or "logged in", warnings
        return False, message or f"`codex login status` exited with {exit.returncode}", warnings


def _unwrap_shell(command: str) -> str:
    match = _SHELL_WRAPPER.match(command.strip())
    if not match:
        return command
    inner = match.group("cmd").strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner
