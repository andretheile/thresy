"""Logging for the services and the ``coach`` commands, with secrets kept out of it.

Every ``coach`` command, and so every service (``migrate``, ``sync``, ``bridge``, ``backup``),
calls :func:`configure` once, from the root callback in :mod:`thresy.cli`. Two layers keep
secrets out of what the process writes:

1. **Request logging of HTTP clients is off.** Loggers of client libraries that write request
   URLs (python-telegram-bot puts the bot token into every URL) or headers are held at a fixed
   level, also with ``--verbose``; see :data:`QUIET_LOGGERS`.
2. **Every record is redacted before it is written.** A filter on the handlers replaces each
   record by a copy whose message, traceback and stack hold no secret: known secret values
   (taken from the environment and the ``.env`` file, or registered at run time with
   :func:`register_secret`) and anything shaped like a Telegram bot token, credentials in a
   URL, a sensitive query parameter (``ticket=``, ``token=`` ...), a bearer token, a JWT, an
   API key or a password assignment. Uncaught exceptions (``sys.excepthook``,
   ``threading.excepthook``) and logging's last-resort handler, which serves loggers that do
   not propagate, go through the same redaction.

:func:`redact` is also used for error texts that leave the process another way: stored in the
database, shown to the athlete, or printed by :func:`thresy.cli.fail`.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

MASK = "<redacted>"

FORMAT = "%(levelname)s %(name)s: %(message)s"

MIN_SECRET_LENGTH = 6
"""Known values shorter than this are not replaced literally (they would hit ordinary words);
the patterns below still apply to them."""

QUIET_LOGGERS: dict[str, int] = {
    # INFO: 'HTTP Request: POST https://api.telegram.org/bot<token>/getUpdates "200 OK"' for
    # every Bot API call, the long poll included (httpx 0.28).
    "httpx": logging.WARNING,
    # DEBUG: connection and request traces, response headers (httpcore 1.0).
    "httpcore": logging.WARNING,
    # DEBUG: every request line with path and query; INFO: redirects with full URLs, such as
    # Garmin's login redirects that carry a service ticket (urllib3 2, used by requests).
    "urllib3": logging.WARNING,
    # DEBUG: "Set Bot API URL: https://api.telegram.org/bot<token>" when a bot is created, and
    # the parameters of every call, i.e. the athlete's messages (python-telegram-bot 22).
    "telegram.Bot": logging.INFO,
    "telegram.ext.ExtBot": logging.INFO,
}

# -- patterns ---------------------------------------------------------------------------------

# "scheme://", or a bare "//" (a mistyped "postgresql//user:password@host" is still one).
_SCHEME = r"((?:\b[A-Za-z][A-Za-z0-9+.-]*:)?//)"
# scheme://user:password@host (the password may hold an unencoded "@": the last one counts)
_URL_PASSWORD = re.compile(_SCHEME + r"([^\s/?#@:]*):([^\s/?#]*)@")
# scheme://token@host
_URL_USER = re.compile(_SCHEME + r"([^\s/?#@:]+)@")
_PASSWORD_ASSIGNMENT = re.compile(
    r"(?i)(passw(?:or)?d[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&)}\]]+)"
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_URL_PASSWORD, rf"\1\2:{MASK}@"),
    (_URL_USER, rf"\1{MASK}@"),
    # A Telegram bot token: the bot id, a colon (perhaps percent-encoded), 35 characters.
    (re.compile(r"(?<![0-9])[0-9]{5,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}"), MASK),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|id_token|token|ticket|code|password|passwd"
            r"|pwd|secret|client_secret|api_key|apikey|key|signature|sig)=)[^&#\s'\"]+"
        ),
        rf"\1{MASK}",
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), rf"\1{MASK}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), MASK),
    # API keys of the model providers (sk-ant-..., sk-proj-..., sk-...).
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), MASK),
    (_PASSWORD_ASSIGNMENT, rf"\1{MASK}"),
    # NAME_TOKEN=..., NAME_SECRET=..., NAME_API_KEY=... (an environment dump)
    (re.compile(r"\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|API_KEY)=)[^\s\"']+"), rf"\1{MASK}"),
]

# Environment variables whose value is a secret (COACH_TELEGRAM_TOKEN, COACH_GARMIN_PASSWORD,
# CLAUDE_CODE_OAUTH_TOKEN, CODEX_API_KEY, DB_APP_PASSWORD, ...). COACH_GARMIN_TOKEN_DIR is not.
_SECRET_NAME = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY)$", re.I)
# ... and whose value as a whole is one when it holds credentials (COACH_DATABASE_URL, ...):
# libpq quotes a connection string it cannot parse in full in its error message.
_URL_NAME = re.compile(r"(?:^|_)URL(?:_|$)", re.I)
_USER_ASSIGNMENT = re.compile(r"\buser\s*=\s*['\"]?([^\s'\"]+)")

# -- known secrets ----------------------------------------------------------------------------

_lock = threading.Lock()
_secrets: set[str] = set()
_known: re.Pattern[str] | None = None


def register_secret(*values: str | None) -> None:
    """Never write these values (nor their percent-encoded forms) to a log from now on."""
    global _known
    added = set()
    for value in values:
        if not value:
            continue
        for form in (value, quote(value, safe=""), unquote(value)):
            if len(form) >= MIN_SECRET_LENGTH and form not in _secrets:
                added.add(form)
    if not added:
        return
    with _lock:
        _secrets.update(added)
        ordered = sorted(_secrets, key=len, reverse=True)  # the longest form wins
        _known = re.compile("|".join(re.escape(s) for s in ordered))


def secrets_from_environ(environ: Mapping[str, str]) -> set[str]:
    """The secret values in an environment: variables named like secrets, passwords in URLs
    and in ``key=value`` connection strings (except one equal to its user name, as in a
    development set-up), and ``*_URL`` values holding a password as a whole."""
    found: set[str] = set()
    for name, value in environ.items():
        if not value:
            continue
        if _SECRET_NAME.search(name):
            found.add(value)
        for match in _URL_PASSWORD.finditer(value):
            user, password = match.group(2), match.group(3)
            if password and unquote(password) != unquote(user):
                found.add(password)
                if _URL_NAME.search(name):
                    found.add(value)
        users = set(_USER_ASSIGNMENT.findall(value))
        for match in _PASSWORD_ASSIGNMENT.finditer(value):
            password = match.group(2).strip("'\"")
            if password and password not in users:
                found.add(password)
                if _URL_NAME.search(name):
                    found.add(value)
    return found


def _environment() -> dict[str, str]:
    """``os.environ`` plus the ``.env`` file the settings read (environment wins)."""
    values: dict[str, str] = {}
    with contextlib.suppress(Exception):
        if Path(".env").is_file():
            from dotenv import dotenv_values

            values.update({k: v for k, v in dotenv_values(".env").items() if v})
    values.update(os.environ)
    return values


def redact(text: str) -> str:
    """``text`` without known secrets and without anything shaped like a credential."""
    if not text:
        return text
    known = _known
    if known is not None:
        text = known.sub(MASK, text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# -- logging ----------------------------------------------------------------------------------


class RedactingFilter(logging.Filter):
    """Hands the handler a copy of each record with message, traceback and stack redacted.

    The copy (not the record itself) is changed, so other handlers see the original."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        clean = copy.copy(record)
        try:
            message = record.getMessage()
        except Exception:  # bad arguments: logging would print them, unredacted, as an error
            message = f"{_safe_str(record.msg)} (the log arguments could not be formatted)"
        clean.msg = redact(message)
        clean.args = None
        if record.exc_info:
            text = record.exc_text or self._formatter.formatException(record.exc_info)
            clean.exc_text = redact(text)
            clean.exc_info = None  # no formatter can render the raw traceback again
        elif record.exc_text:
            clean.exc_text = redact(record.exc_text)
        if record.stack_info:
            clean.stack_info = redact(record.stack_info)
        return clean


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unprintable {type(value).__name__}>"


class StderrHandler(logging.StreamHandler):
    """Writes to ``sys.stderr`` as it is when a record arrives (like logging's own
    last-resort handler), so a replaced stream is followed."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        logging.Handler.__init__(self, level)

    @property
    def stream(self) -> Any:  # type: ignore[override]
        return sys.stderr


def _redacting(handler: logging.Handler) -> logging.Handler:
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())
    return handler


def _write_redacted(text: str) -> None:
    try:
        sys.stderr.write(redact(text))
        sys.stderr.flush()
    except Exception:
        pass  # never fall back to printing the text as it is


def excepthook(exc_type: Any, exc: Any, tb: Any) -> None:
    """``sys.excepthook``: the usual traceback, redacted."""
    _write_redacted("".join(traceback.format_exception(exc_type, exc, tb)))


def thread_excepthook(args: Any) -> None:
    """``threading.excepthook``: the usual report, redacted."""
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else threading.get_ident()
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _write_redacted(f"Exception in thread {name}:\n{text}")


def _default_hook(hook: Any, default: Any) -> bool:
    # typer installs a hook that, without its "pretty exceptions", calls the default one.
    return hook is default or getattr(hook, "__module__", None) == "typer.main"


def configure(*, verbose: bool = False, environ: Mapping[str, str] | None = None) -> None:
    """Set up logging for this process (idempotent).

    Like ``logging.basicConfig``: unless the root logger already has handlers (as under
    pytest), it gets INFO (DEBUG with ``verbose``) and a handler writing :data:`FORMAT` to
    standard error. Then every root handler and the last-resort handler get a
    :class:`RedactingFilter`, the loggers in :data:`QUIET_LOGGERS` their fixed level, and the
    default hooks for uncaught exceptions are replaced by redacting ones. Secrets are
    collected from ``environ`` (default: the environment and the ``.env`` file).
    """
    register_secret(*secrets_from_environ(_environment() if environ is None else environ))
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.DEBUG if verbose else logging.INFO)
        handler = StderrHandler()
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    for handler in root.handlers:
        _redacting(handler)
    for name, level in QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(level)
    if not isinstance(logging.lastResort, StderrHandler):
        logging.lastResort = _redacting(StderrHandler(logging.WARNING))
    if _default_hook(sys.excepthook, sys.__excepthook__):
        sys.excepthook = excepthook
    if _default_hook(threading.excepthook, threading.__excepthook__):
        threading.excepthook = thread_excepthook
