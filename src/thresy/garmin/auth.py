"""Logging in to Garmin Connect: saved tokens first, email and password only when needed.

Mechanics only; *when* a password login may be attempted is decided by the caller
(:mod:`thresy.sync.auth`), because Garmin locks accounts after repeated password logins.

Files in the token directory (``COACH_GARMIN_TOKEN_DIR``, mode 0700, every file 0600):

* ``garmin_tokens.json``: the session tokens, written by ``garminconnect`` itself
  (``Client.dump``). When the library refreshes the access token during a request it rewrites
  this file (it remembers the path from ``login(tokenstore)``).
* ``mfa_pending.json``: a password login that waits for a verification code. It holds the SSO
  session cookies that ``Garmin.resume_login`` needs to finish the login in another process, so
  it is as sensitive as a password. It is deleted when the login completes.

Nothing here touches the database.

``garminconnect`` 0.3.20 calls used (read from the installed source):

* ``Garmin()`` / ``Garmin(email=..., password=..., return_on_mfa=True)``;
* ``Garmin.login(tokenstore)``: loads ``<dir>/garmin_tokens.json``, refreshes it if it is about
  to expire, and loads the profile. Without credentials on the instance, a missing or rejected
  token raises ``GarminConnectAuthenticationError`` instead of falling back to a password login;
* ``Garmin.login()`` with ``return_on_mfa=True``: a password login that returns
  ``("needs_mfa", client_state)`` when Garmin asks for a code, or ``(None, None)``;
* ``Garmin.resume_login(client_state, code)``: finishes it, also on a new instance;
* ``Garmin.client.dump(path)``: writes the tokens (0600 file in a 0700 directory).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from thresy.garmin.client import ConnectClient, translate_error
from thresy.garmin.errors import GarminAuthError, GarminError
from thresy.logs import redact, register_secret

TOKEN_FILE = "garmin_tokens.json"  # the file name garminconnect uses inside a token directory
MFA_FILE = "mfa_pending.json"
MFA_PENDING_TTL = timedelta(hours=1)
"""How long a waiting login is offered for completion. Garmin's own limit is unknown; after
this the login has to be started again."""

GarminFactory = Callable[..., Any]


def _default_factory(**kwargs: Any) -> Any:
    from garminconnect import Garmin

    return Garmin(**kwargs)


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode 0600 (the directory becomes 0700)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class TokenMissing(GarminAuthError):
    """There is no saved session in the token directory."""


@dataclass(frozen=True)
class PendingMfa:
    """A password login waiting for a verification code."""

    created_at: datetime
    method: str | None  # how Garmin sent the code, e.g. "email"; None if unknown
    client_state: dict[str, Any]

    def expired(self, now: datetime) -> bool:
        return now - self.created_at > MFA_PENDING_TTL


class TokenStore:
    """The token directory of the sync service."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @property
    def token_file(self) -> Path:
        return self.directory / TOKEN_FILE

    @property
    def mfa_file(self) -> Path:
        return self.directory / MFA_FILE

    def has_token(self) -> bool:
        try:
            return self.token_file.is_file() and self.token_file.stat().st_size > 0
        except OSError:
            return False

    def token_modified_at(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.token_file.stat().st_mtime, tz=UTC)
        except OSError:
            return None

    def ensure_private(self) -> None:
        """Create the directory (0700) and tighten modes of existing files to 0600."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.directory.chmod(0o700)
        for path in (self.token_file, self.mfa_file):
            if path.is_file() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    path.chmod(0o600)

    def save_pending_mfa(self, pending: PendingMfa) -> None:
        data = {
            "created_at": pending.created_at.isoformat(),
            "method": pending.method,
            "client_state": pending.client_state,
        }
        write_private(self.mfa_file, json.dumps(data))

    def load_pending_mfa(self) -> PendingMfa | None:
        """The waiting login, or ``None`` (also for an unreadable or malformed file)."""
        try:
            data = json.loads(self.mfa_file.read_text(encoding="utf-8"))
            return PendingMfa(
                created_at=datetime.fromisoformat(data["created_at"]),
                method=data.get("method"),
                client_state=dict(data["client_state"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def clear_pending_mfa(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.mfa_file.unlink()


@dataclass
class LoginStarted:
    """Result of :meth:`GarminLogin.start_password_login`: logged in, or waiting for a code."""

    client: ConnectClient | None
    pending: PendingMfa | None
    garmin: Any = None
    """The instance holding the waiting login; an interactive caller can finish it with
    :meth:`GarminLogin.complete_mfa` without going through the file."""


class GarminLogin:
    """Token login, password login and the two-step verification-code flow.

    ``factory`` builds ``garminconnect.Garmin`` objects (keyword arguments as the library's
    constructor); tests pass a stub.
    """

    def __init__(self, store: TokenStore, *, factory: GarminFactory | None = None) -> None:
        self.store = store
        self.factory = factory or _default_factory

    def token_login(self) -> ConnectClient:
        """Log in with the saved tokens.

        Raises :class:`TokenMissing` without a token file, :class:`GarminAuthError` when Garmin
        rejects the token, :class:`~thresy.garmin.errors.GarminRateLimited` or
        :class:`GarminError` for anything else. Never sends the password.
        """
        if not self.store.has_token():
            raise TokenMissing(f"no saved Garmin session in {self.store.directory}")
        self.store.ensure_private()
        garmin = self.factory()  # no credentials: no silent password login inside the library
        try:
            garmin.login(str(self.store.directory))
        except Exception as exc:
            raise _as_garmin_error(exc) from exc
        self.store.ensure_private()  # the library may have rewritten the file (refresh)
        return ConnectClient(garmin)

    def start_password_login(self, email: str, password: str, *, now: datetime) -> LoginStarted:
        """One password login. Saves the tokens on success; on a verification-code request it
        saves the waiting login to ``mfa_pending.json`` and returns it.

        Raises :class:`GarminAuthError` (wrong credentials, locked account),
        :class:`~thresy.garmin.errors.GarminRateLimited` or :class:`GarminError`.
        """
        register_secret(password)  # e.g. typed in by `coach sync login`, not in the environment
        self.store.ensure_private()
        self.store.clear_pending_mfa()
        garmin = self.factory(email=email, password=password, return_on_mfa=True)
        try:
            status, state = garmin.login()
        except Exception as exc:
            raise _as_garmin_error(exc) from exc
        if status == "needs_mfa":
            method = state.get("method") if isinstance(state, dict) else None
            pending = PendingMfa(created_at=now, method=method, client_state=dict(state))
            self.store.save_pending_mfa(pending)
            return LoginStarted(client=None, pending=pending, garmin=garmin)
        self._save_tokens(garmin)
        # In return_on_mfa mode the library skips loading the profile; a token login does it.
        return LoginStarted(client=self.token_login(), pending=None)

    def complete_mfa(self, code: str, *, now: datetime, garmin: Any = None) -> ConnectClient:
        """Finish the waiting login with the verification ``code``.

        ``garmin`` is the instance that started the login (interactive use); otherwise the
        login is rebuilt from ``mfa_pending.json``. A wrong code leaves the waiting login in
        place so the athlete can try again. Raises :class:`GarminAuthError` when there is
        nothing to complete or Garmin refuses the code.
        """
        code = code.strip()
        if not code:
            raise GarminAuthError("empty verification code")
        pending = self.store.load_pending_mfa()
        if garmin is None:
            if pending is None:
                raise GarminAuthError("no Garmin login is waiting for a code")
            if pending.expired(now):
                self.store.clear_pending_mfa()
                raise GarminAuthError("the waiting Garmin login has expired; start a new one")
            garmin = self.factory(return_on_mfa=True)
        state = pending.client_state if pending else {}
        try:
            garmin.resume_login(state, code)
        except Exception as exc:
            raise _as_garmin_error(exc) from exc
        self._save_tokens(garmin)
        self.store.clear_pending_mfa()
        return ConnectClient(garmin)

    def _save_tokens(self, garmin: Any) -> None:
        try:
            garmin.client.dump(str(self.store.directory))
        except Exception as exc:
            raise GarminError(redact(f"could not save the Garmin tokens: {exc}")) from exc
        self.store.ensure_private()


def _as_garmin_error(exc: Exception) -> Exception:
    translated = translate_error(exc)
    if isinstance(translated, GarminError):
        return translated
    # Anything else from the login (a ValueError for a bad token path, ...) is a failed login.
    return GarminError(redact(f"{type(exc).__name__}: {exc}"))
