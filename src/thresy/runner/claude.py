"""Claude Code adapter (verified against Claude Code 2.1.327).

Command line (prompt on stdin, working directory = RunRequest.cwd):

    claude -p --output-format stream-json --verbose
           [--model <model>] [--resume <session_id>]
           [--append-system-prompt-file <private temporary file>]
           --permission-mode bypassPermissions --permission-prompts none
           [extra args]

No personal text is on the command line: the prompt goes to stdin and
RunRequest.system_append to a file only this user can read, removed after the
run. `--bare` is never passed: bare mode ignores CLAUDE_CODE_OAUTH_TOKEN, which
is how this deployment reaches the model through a subscription.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, ClassVar

from .classify import classify_text, kind_from_http_status, summarize_tool
from .cli_base import CliRunner, Invocation, StreamParser, private_file, usage_dict
from .config import HarnessConfig
from .core import ErrorKind, Progress, RunEvent, RunnerCapabilities, RunRequest, RunResult, Started
from .isolation import UserSwitch
from .process import ProcessExit

log = logging.getLogger(__name__)

# SDKAssistantMessageError / system api_retry "error" values -> error kind.
_CODE_KIND: dict[str, ErrorKind] = {
    "authentication_failed": "auth",
    "oauth_org_not_allowed": "auth",
    "account_on_hold": "auth",
    "cloud_credential_error": "auth",
    "rate_limit": "rate_limit",
    "overloaded": "rate_limit",
    "billing_error": "rate_limit",
}
# Anthropic API error types as reported in result.api_error_type.
_API_TYPE_KIND: dict[str, ErrorKind] = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "rate_limit_error": "rate_limit",
    "overloaded_error": "rate_limit",
    "billing_error": "rate_limit",
}
_USAGE_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}
_TRUTHY = {"1", "true", "yes", "on"}
EXPIRY_WARNING = timedelta(days=3)


class ClaudeStreamParser(StreamParser):
    """Maps Claude Code's stream-json output to run events."""

    def __init__(self, runner: str, req: RunRequest, model: str | None) -> None:
        super().__init__(runner, req, model)
        self._result: dict[str, Any] | None = None
        self._api_error: str | None = None
        self._api_error_status: Any = None
        self._api_error_text: str | None = None
        self._retry_error: str | None = None
        self._retry_status: Any = None
        self._rate_limit_rejected: dict[str, Any] | None = None

    def feed(self, obj: dict[str, Any]) -> list[RunEvent]:
        kind = obj.get("type")
        if kind == "system":
            return self._system(obj)
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                self._rate_limit_rejected = info
            return []
        if kind == "result":
            self._result = obj
            self.terminal_seen = True
            if isinstance(obj.get("session_id"), str):
                self.session_id = obj["session_id"]
            return []
        # user (tool results), stream_event, and anything new: nothing to show.
        return []

    def _system(self, obj: dict[str, Any]) -> list[RunEvent]:
        subtype = obj.get("subtype")
        if subtype == "init":
            session_id = obj.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
            if isinstance(obj.get("model"), str) and obj["model"]:
                self.model = obj["model"]
            return [Started(self.session_id)]
        if subtype == "api_retry":
            self._retry_error = obj.get("error")
            self._retry_status = obj.get("error_status")
        return []

    def _assistant(self, obj: dict[str, Any]) -> list[RunEvent]:
        if obj.get("parent_tool_use_id"):
            return []  # sub-agent traffic; the main thread reports the Task call
        message = obj.get("message") or {}
        content = message.get("content") or []
        if obj.get("error"):
            # A synthetic message carrying an API error, not agent output.
            self._api_error = str(obj["error"])
            self._api_error_status = obj.get("api_error_status")
            self._api_error_text = _joined_text(content)
            return []
        events: list[RunEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    self.last_text = text
                    events.append(Progress("text", text))
            elif block.get("type") in ("tool_use", "server_tool_use"):
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                events.append(
                    Progress("tool", summarize_tool(str(block.get("name") or "tool"), args))
                )
        return events

    def finish(self, exit: ProcessExit) -> RunResult:
        cost, usage = self._cost_and_usage()
        if exit.cancelled:
            return self.cancelled_result(cost_usd=cost, usage=usage)
        res = self._result
        if res is None:
            return self.failure_from_exit(exit, self._api_error_text, cost_usd=cost, usage=usage)
        self._warn_on_expiry(res.get("credential_expires_at"))
        failed = bool(res.get("is_error")) or res.get("subtype") != "success"
        if not failed:
            text = res.get("result")
            if exit.timed_out:
                log.info("claude: result arrived but the process outlived the timeout")
            return self.result(
                ok=True,
                text=text if isinstance(text, str) else self.last_text,
                cost_usd=cost,
                usage=usage,
            )
        return self.error(self._classify(res), self._detail(res), cost_usd=cost, usage=usage)

    def _classify(self, res: dict[str, Any]) -> ErrorKind:
        kind = _CODE_KIND.get(self._api_error or "")
        if kind:
            return kind
        kind = kind_from_http_status(res.get("api_error_status")) or kind_from_http_status(
            self._api_error_status
        )
        if kind:
            return kind
        kind = _API_TYPE_KIND.get(str(res.get("api_error_type") or ""))
        if kind:
            return kind
        kind = classify_text(_result_text(res), *_errors(res), self._api_error_text)
        if kind:
            return kind
        if self._rate_limit_rejected is not None:
            return "rate_limit"
        if res.get("terminal_reason") == "api_error":
            kind = _CODE_KIND.get(self._retry_error or "") or kind_from_http_status(
                self._retry_status
            )
            if kind:
                return kind
        return "other"

    def _detail(self, res: dict[str, Any]) -> str:
        parts = []
        text = _result_text(res) or self._api_error_text
        if text:
            parts.append(text)
        parts.extend(_errors(res))
        meta = []
        if res.get("subtype") and res.get("subtype") != "success":
            meta.append(str(res["subtype"]))
        if res.get("terminal_reason"):
            meta.append(f"terminal_reason={res['terminal_reason']}")
        if res.get("api_error_status"):
            meta.append(f"status={res['api_error_status']}")
        if self._api_error:
            meta.append(f"error={self._api_error}")
        if self._rate_limit_rejected and self._rate_limit_rejected.get("resetsAt"):
            meta.append(f"resets_at={self._rate_limit_rejected['resetsAt']}")
        if meta:
            parts.append("(" + ", ".join(meta) + ")")
        return " ".join(parts) or "Claude Code reported an error"

    def _cost_and_usage(self) -> tuple[float | None, dict | None]:
        res = self._result
        if res is None:
            return None, None
        # On resume, total_cost_usd includes earlier runs of the session;
        # model_usage_since_last_result covers this run only.
        cost: float | None = None
        since = res.get("model_usage_since_last_result")
        if isinstance(since, dict):
            cost = round(
                sum(_num(v.get("costUSD")) for v in since.values() if isinstance(v, dict)), 8
            )
        elif isinstance(res.get("total_cost_usd"), (int, float)):
            cost = float(res["total_cost_usd"])
        raw = res.get("usage")
        if isinstance(raw, dict):
            raw = {k: v for k, v in raw.items() if k != "iterations"}
        return cost, usage_dict(raw, _USAGE_KEYS)

    @staticmethod
    def _warn_on_expiry(value: Any) -> None:
        expires = _parse_time(value)
        if expires and expires - datetime.now(UTC) < EXPIRY_WARNING:
            log.warning("claude: the login expires at %s; renew it", value)


class ClaudeRunner(CliRunner):
    name = "claude"
    capabilities = RunnerCapabilities(
        supports_resume=True, supports_streaming=True, supports_system_append=True
    )
    default_binaries = ("claude",)
    default_models: ClassVar[Mapping[str, str | None]] = MappingProxyType(
        {"fast": "haiku", "smart": "sonnet"}
    )
    default_permission_args = (
        "--permission-mode",
        "bypassPermissions",
        "--permission-prompts",
        "none",
    )
    # Startup failures then also end with a structured result message.
    base_env: ClassVar[Mapping[str, str]] = MappingProxyType(
        {"CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "1"}
    )
    # Auth (CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ...)
    # and configuration (CLAUDE_CONFIG_DIR, CLAUDE_CODE_*) per the env-vars docs.
    env_prefixes = ("ANTHROPIC_", "CLAUDE_")
    env_blocked = frozenset({"CLAUDE_CODE_SIMPLE"})  # bare mode, see module docstring
    verified = True

    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        kill_grace_s: float | None = None,
        switch: UserSwitch | None = None,
    ):
        super().__init__(config, kill_grace_s=kill_grace_s, switch=switch)
        if "--bare" in (*self.config.extra_args, *self.permission_args):
            raise ValueError(
                "--bare is not allowed: bare mode ignores CLAUDE_CODE_OAUTH_TOKEN "
                "(subscription auth)"
            )
        if str(self.config.env.get("CLAUDE_CODE_SIMPLE", "")).lower() in _TRUTHY:
            raise ValueError("CLAUDE_CODE_SIMPLE enables bare mode and is not allowed")

    def build_invocation(self, req: RunRequest, binary: str, model: str | None) -> Invocation:
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        if req.session_id:
            argv += ["--resume", req.session_id]
        temp_dirs: tuple = ()
        if req.system_append:
            directory, path = private_file(
                req.system_append, "system-append.md", group=self.private_group
            )
            temp_dirs = (directory,)
            argv += ["--append-system-prompt-file", str(path)]
        argv += [*self.permission_args, *self.config.extra_args]
        return Invocation(argv, stdin=req.prompt.encode("utf-8"), temp_dirs=temp_dirs)

    def new_parser(self, req: RunRequest, model: str | None) -> ClaudeStreamParser:
        return ClaudeStreamParser(self.name, req, model)

    async def check_auth(
        self, binary: str, env: Mapping[str, str]
    ) -> tuple[bool | None, str, list[str]]:
        warnings: list[str] = []
        if str(env.get("CLAUDE_CODE_SIMPLE", "")).lower() in _TRUTHY:
            warnings.append(
                "CLAUDE_CODE_SIMPLE is set: bare mode ignores CLAUDE_CODE_OAUTH_TOKEN "
                "and the stored login"
            )
        if env.get("ANTHROPIC_API_KEY"):
            warnings.append(
                "ANTHROPIC_API_KEY is set and takes precedence over the subscription "
                "(CLAUDE_CODE_OAUTH_TOKEN or login)"
            )
        exit, out = await self.capture([binary, "auth", "status", "--json"], env)
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            ok = None if exit.returncode not in (0, 1) else exit.returncode == 0
            return ok, f"`claude auth status` gave no JSON (exit {exit.returncode})", warnings
        method = str(data.get("authMethod") or "none")
        provider = data.get("apiProvider")
        if not data.get("loggedIn") or exit.returncode == 1:
            return (
                False,
                "not logged in: set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) "
                "or run `claude auth login`",
                warnings,
            )
        detail = f"logged in via {method}" + (f" ({provider})" if provider else "")
        expires = _parse_time(data.get("loginExpiresAt"))
        if expires is not None:
            now = datetime.now(UTC)
            if expires <= now:
                return (
                    False,
                    f"{detail}, but the login expired at {data['loginExpiresAt']}",
                    warnings,
                )
            if expires - now < EXPIRY_WARNING:
                warnings.append(f"login expires at {data['loginExpiresAt']}; renew it soon")
            detail += f", login valid until {data['loginExpiresAt']}"
        if method == "oauth_token":
            detail += "; token validity is only checked by a model request"
        return True, detail, warnings


def _joined_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def _result_text(res: dict[str, Any]) -> str:
    text = res.get("result")
    return text.strip() if isinstance(text, str) else ""


def _errors(res: dict[str, Any]) -> list[str]:
    errors = res.get("errors")
    if not isinstance(errors, list):
        return []
    return [str(e) for e in errors if e]


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
