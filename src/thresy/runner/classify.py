"""Harness-independent helpers: error classification from text, tool summaries.

Adapters classify from structured fields first (error codes, HTTP statuses)
and fall back to `classify_text` for messages that only exist as text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .core import ErrorKind

_AUTH_PATTERNS = [
    r"not logged in",
    r"please (run )?/?login",
    r"please log ?in",
    r"log ?in again",
    r"re-?authenticate",
    r"failed to authenticate",
    r"authentication (failed|required|error)",
    r"not authenticated",
    r"login (has )?expired",
    r"login reached its time limit",
    r"oauth (access )?token (has expired|is invalid|revoked|expired)",
    r"oauth session expired",
    r"invalid api key",
    r"invalid (x-api-key|authentication credentials|bearer token)",
    r"(access|refresh) token (could not be refreshed|has expired|expired|is invalid)",
    r"\bunauthori[sz]ed\b",
    r"(api error|status|http)\W{0,3}(401|403)\b",
    r"\b401\b[^\n]{0,40}(unauthori[sz]ed|auth|token|credential)",
    r"your account is on hold",
    r"organization has been disabled",
    r"does not have access to claude",
]

_RATE_PATTERNS = [
    r"hit your [\w' ]{0,40}limit",
    r"usage limit",
    r"spend limit",
    r"rate[ _-]?limit",
    r"too many requests",
    r"temporarily limiting requests",
    r"\boverloaded\b",
    r"(api error|status|http)\W{0,3}(429|529)\b",
    r"\b(429|529)\b[^\n]{0,40}(too many|rate|overload|capacity|rejected)",
    r"credit balance (is )?too low",
    r"exceeded your current quota",
    r"insufficient[_ ]quota",
    r"out of credits",
    r"usage credits required",
]

_AUTH_RE = re.compile("|".join(f"(?:{p})" for p in _AUTH_PATTERNS), re.IGNORECASE)
_RATE_RE = re.compile("|".join(f"(?:{p})" for p in _RATE_PATTERNS), re.IGNORECASE)


def classify_text(*texts: str | None) -> ErrorKind | None:
    """Return "auth" or "rate_limit" if any text clearly says so, else None."""
    joined = "\n".join(t for t in texts if t)
    if not joined:
        return None
    if _AUTH_RE.search(joined):
        return "auth"
    if _RATE_RE.search(joined):
        return "rate_limit"
    return None


def kind_from_http_status(status: Any) -> ErrorKind | None:
    if status in (401, 403):
        return "auth"
    if status in (429, 529):
        return "rate_limit"
    return None


# -- tool summaries ----------------------------------------------------------

_ARG_KEYS = (
    "command",
    "cmd",
    "file_path",
    "path",
    "notebook_path",
    "filePath",
    "pattern",
    "glob",
    "url",
    "query",
    "skill",
    "subagent_type",
    "description",
    "prompt",
)

MAX_TARGET = 60


def truncate(text: str, limit: int = MAX_TARGET) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def summarize_tool(name: str, args: Mapping[str, Any] | None) -> str:
    """One short line for a tool call: the tool name and a truncated target.
    Never includes tool output."""
    name = _tool_display_name(name)
    target = _first_target(args or {})
    return f"{name}: {truncate(target)}" if target else name


def _tool_display_name(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"{parts[1]}/{'__'.join(parts[2:])}"
    return name


def _first_target(args: Mapping[str, Any]) -> str:
    for key in _ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(value)
    return ""


def join_texts(parts: Iterable[str]) -> str:
    return "\n".join(p for p in parts if p)
