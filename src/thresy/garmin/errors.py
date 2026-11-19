"""Errors of the Garmin layer, independent of the ``garminconnect`` library's own classes."""

from __future__ import annotations


class GarminError(RuntimeError):
    """A Garmin request failed (network, server error, unexpected answer)."""


class GarminNotFound(GarminError):
    """The requested resource does not exist (HTTP 404), e.g. an activity without a file."""


class GarminRateLimited(GarminError):
    """Garmin answered 429 (or the SSO's 427): slow down before the next request."""


class GarminAuthError(GarminError):
    """The session was rejected (expired or revoked token, wrong credentials, locked account)."""


class GarminAuthRequired(GarminAuthError):
    """No usable session, and logging in needs a human.

    ``status`` is one of ``no_credentials``, ``mfa_required``, ``login_failed``,
    ``login_paused``; ``reason`` is a sentence the bridge can show the athlete.
    """

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class GarminMfaRequired(GarminAuthRequired):
    """Garmin sent a verification code; the login waits for ``coach sync login --code``."""

    def __init__(self, reason: str, *, method: str | None = None) -> None:
        super().__init__("mfa_required", reason)
        self.method = method
