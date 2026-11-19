"""When the sync may log in to Garmin, and how a human completes a login.

Garmin locks accounts and rate-limits after repeated password logins, so the rules are:

* The saved token is always tried first. A rate limit or a network error during the token
  login never leads to a password login.
* A password login runs only when the token is missing or rejected, credentials are
  configured, and no earlier attempt is pending: before each attempt
  ``password_login_paused_until`` is set ``COACH_SYNC_LOGIN_RETRY_H`` ahead and only a success
  clears it. One failure therefore means: wait for a human (``coach sync login``) or the long
  back-off. A crash in the middle of a login counts as a failure too.
* When Garmin asks for a verification code, the waiting login is saved in the token directory
  and ``garmin_auth_required`` tells the athlete to run ``coach sync login --code <code>``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from thresy.garmin.auth import GarminLogin, LoginStarted, TokenMissing, TokenStore
from thresy.garmin.client import GarminClient
from thresy.garmin.errors import (
    GarminAuthError,
    GarminAuthRequired,
    GarminError,
    GarminMfaRequired,
    GarminRateLimited,
)
from thresy.store import Conn
from thresy.sync import state
from thresy.sync.config import SyncConfig

log = logging.getLogger(__name__)

LOGIN_HINT = "coach sync login"
CODE_HINT = "coach sync login --code <code>"


def _when(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def mfa_reason(method: str | None) -> str:
    via = f" by {method}" if method else ""
    return (
        f"Garmin sent a verification code{via}. Complete the login in the sync service with "
        f"`{CODE_HINT}`; until then nothing is synced."
    )


def mark_auth_ok(conn: Conn, now: datetime) -> None:
    state.merge(
        conn,
        state.AUTH,
        status="ok",
        reason=None,
        updated_at=state.iso(now),
        last_ok_at=state.iso(now),
        password_login_paused_until=None,
    )


def mark_auth_problem(
    conn: Conn, now: datetime, status: str, reason: str, *, event: bool = True
) -> GarminAuthRequired:
    """Record the problem, raise ``garmin_auth_required`` (once per UTC day) and return the
    exception to raise."""
    state.merge(conn, state.AUTH, status=status, reason=reason, updated_at=state.iso(now))
    if event:
        state.insert_auth_required(conn, now, reason)
    return GarminAuthRequired(status, reason)


def _pause_password_logins(conn: Conn, now: datetime, config: SyncConfig) -> datetime:
    until = now + timedelta(hours=config.login_retry_h)
    state.merge(
        conn,
        state.AUTH,
        last_password_login_at=state.iso(now),
        password_login_paused_until=state.iso(until),
    )
    return until


class ClientProvider:
    """Hands the sync a logged-in client and caches it between passes.

    ``login`` defaults to the real ``garminconnect`` login over ``config.token_dir``. Tests
    pass a :class:`GarminLogin` with a stub factory, or subclass and override :meth:`get`.
    """

    def __init__(self, config: SyncConfig, *, login: GarminLogin | None = None) -> None:
        self.config = config
        self.login = login or GarminLogin(TokenStore(config.token_dir))
        self._client: GarminClient | None = None

    def invalidate(self) -> None:
        """Forget the cached client (after Garmin rejected it)."""
        self._client = None

    def get(self, conn: Conn, now: datetime) -> GarminClient:
        """A logged-in client. Raises :class:`GarminAuthRequired` when a human must act,
        :class:`GarminRateLimited` or :class:`GarminError` for temporary problems."""
        if self._client is not None:
            return self._client
        try:
            client = self.login.token_login()
        except TokenMissing:
            problem = "there is no saved Garmin session"
        except GarminRateLimited:
            raise
        except GarminAuthError as exc:
            problem = f"Garmin rejected the saved session ({exc})"
        else:
            if state.get(conn, state.AUTH).get("status") != "ok":
                mark_auth_ok(conn, now)
            self._client = client
            return client
        log.warning("token login failed: %s", problem)
        self._client = self._password_login(conn, now, problem)
        return self._client

    def _password_login(self, conn: Conn, now: datetime, problem: str) -> GarminClient:
        config = self.config
        pending = self.login.store.load_pending_mfa()
        if pending is not None and not pending.expired(now):
            raise mark_auth_problem(conn, now, "mfa_required", mfa_reason(pending.method))
        if not config.has_credentials:
            reason = (
                f"No Garmin login: {problem} and no email and password are configured. "
                f"Run `{LOGIN_HINT}` in the sync service."
            )
            raise mark_auth_problem(conn, now, "no_credentials", reason)
        paused = state.parse_time(state.get(conn, state.AUTH).get("password_login_paused_until"))
        if paused is not None and now < paused:
            reason = (
                f"No Garmin login: {problem}, and automatic logins are paused until "
                f"{_when(paused)} after the last attempt. Run `{LOGIN_HINT}` in the sync "
                "service to log in now."
            )
            raise mark_auth_problem(conn, now, "login_paused", reason)

        # Count the attempt before making it: one try, then wait for a human or the back-off.
        until = _pause_password_logins(conn, now, config)
        log.info("logging in to Garmin with email and password")
        try:
            assert config.garmin_email and config.garmin_password
            started = self.login.start_password_login(
                config.garmin_email, config.garmin_password, now=now
            )
        except GarminRateLimited as exc:
            reason = (
                f"Garmin is rate-limiting logins ({exc}). Automatic logins are paused until "
                f"{_when(until)}; run `{LOGIN_HINT}` later to log in sooner."
            )
            raise mark_auth_problem(conn, now, "login_failed", reason) from exc
        except GarminError as exc:
            reason = (
                f"Garmin login failed ({exc}). Check COACH_GARMIN_EMAIL and "
                f"COACH_GARMIN_PASSWORD, then run `{LOGIN_HINT}`. Automatic logins are paused "
                f"until {_when(until)}."
            )
            raise mark_auth_problem(conn, now, "login_failed", reason) from exc
        if started.pending is not None:
            raise mark_auth_problem(conn, now, "mfa_required", mfa_reason(started.pending.method))
        assert started.client is not None
        mark_auth_ok(conn, now)
        return started.client


# Human login (`coach sync login`) -------------------------------------------------------------


@dataclass(frozen=True)
class LoginOutcome:
    status: str  # "ok" (logged in), "token_ok" (saved token works), "mfa_required"
    message: str


def human_login(
    config: SyncConfig,
    *,
    conn: Conn | None,
    now: datetime,
    code: str | None = None,
    restart: bool = False,
    email: str | None = None,
    password: str | None = None,
    prompt_code: Callable[[str | None], str] | None = None,
    login: GarminLogin | None = None,
) -> LoginOutcome:
    """What ``coach sync login`` does. ``conn`` may be ``None`` (no database yet): the outcome
    is then not recorded in ``sync_state``.

    * ``code``: complete the waiting login.
    * otherwise the saved token is checked first (unless ``restart``); only if it does not
      work is a password login started. ``prompt_code`` (interactive use) is asked for the
      verification code right away; without it the waiting login is saved for ``--code``.

    Raises :class:`GarminAuthError` / :class:`GarminError` with a readable message on failure.
    """
    login = login or GarminLogin(TokenStore(config.token_dir))
    if code is not None:
        try:
            login.complete_mfa(code, now=now)
        except GarminError as exc:
            if conn is not None:
                state.merge(conn, state.AUTH, reason=str(exc), updated_at=state.iso(now))
            raise
        if conn is not None:
            mark_auth_ok(conn, now)
        return LoginOutcome("ok", "Logged in to Garmin; the session is saved.")

    pending = login.store.load_pending_mfa()
    if pending is not None and not pending.expired(now) and not restart:
        raise GarminMfaRequired(
            "A Garmin login is waiting for a verification code: run "
            f"`{CODE_HINT}`, or `{LOGIN_HINT} --restart` to start a new login.",
            method=pending.method,
        )
    if not restart:
        try:
            login.token_login()
        except (TokenMissing, GarminAuthError) as exc:
            log.info("saved session not usable (%s); logging in with email and password", exc)
        else:
            if conn is not None:
                mark_auth_ok(conn, now)
            return LoginOutcome("token_ok", "The saved Garmin session works; nothing to do.")

    email = email or config.garmin_email
    password = password or config.garmin_password
    if not (email and password):
        raise GarminAuthRequired(
            "no_credentials",
            "No Garmin email and password: set COACH_GARMIN_EMAIL and COACH_GARMIN_PASSWORD, "
            "or run this command in a terminal to be asked for them.",
        )
    if conn is not None:
        _pause_password_logins(conn, now, config)
    try:
        started: LoginStarted = login.start_password_login(email, password, now=now)
        if started.pending is not None and prompt_code is not None:
            login.complete_mfa(prompt_code(started.pending.method), now=now, garmin=started.garmin)
            started = LoginStarted(client=None, pending=None)
    except GarminError as exc:
        if conn is not None:
            state.merge(
                conn, state.AUTH, status="login_failed", reason=str(exc), updated_at=state.iso(now)
            )
        raise
    if started.pending is not None:
        reason = mfa_reason(started.pending.method)
        if conn is not None:
            state.merge(conn, state.AUTH, status="mfa_required", reason=reason)
        return LoginOutcome("mfa_required", reason)
    if conn is not None:
        mark_auth_ok(conn, now)
    return LoginOutcome("ok", "Logged in to Garmin; the session is saved.")
