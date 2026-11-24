"""Settings of the web dashboard (``COACH_WEB_*``), on top of the core settings.

The read-only database URL (``COACH_DATABASE_URL_RO``, role ``coach_ro``), the time zone
(``COACH_TZ``), the language (``COACH_LANGUAGE``) and the coach's name (``COACH_NAME``) come
from :class:`thresy.config.Settings`. Nothing here has a usable default for the two secrets:
without a password hash and a session secret the service refuses to start.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG, ConfigError
from thresy.logs import register_secret

MIN_SECRET_LENGTH = 32
"""``COACH_WEB_SECRET`` must have at least this many characters (``openssl rand -hex 32``
gives 64)."""

LOOPBACK = "127.0.0.1"


class WebSettings(BaseSettings):
    """``COACH_WEB_*`` environment variables."""

    model_config = SETTINGS_CONFIG

    web_password_hash: SecretStr | None = None
    """The athlete's password as a salted scrypt hash (``coach web password`` prints one)."""
    web_secret: SecretStr | None = None
    """Key that signs the session cookies (at least 32 characters). Changing it ends every
    session."""
    web_host: str = "0.0.0.0"
    """Address the server listens on. In the container it listens on the Compose network;
    the port is never published, the reverse proxy reaches it there."""
    web_port: int = Field(8080, ge=1, le=65535)
    web_session_hours: float = Field(336, gt=0, le=24 * 90)
    """How long a login lasts (default 14 days). The expiry is inside the signed cookie."""
    web_client_ip_header: str | None = None
    """Header that carries the client's address, set by the reverse proxy (for example
    ``X-Forwarded-For``, ``X-Real-IP`` or ``CF-Connecting-IP``). Unset: the address of the
    connection, which behind a proxy is the proxy's. Name a header only when every request
    reaches the service through a proxy that sets it, or clients can choose their address."""
    web_client_ip_hops: int = Field(1, ge=1, le=10)
    """For a list-valued header such as ``X-Forwarded-For``: take the address this many
    entries from the right (1 = the one your own proxy appended, which a client cannot
    forge)."""
    web_dev_http: bool = False
    """Development only: session cookies without ``Secure`` so that the login works over plain
    HTTP. The server then listens on 127.0.0.1 only."""

    @field_validator("web_client_ip_header")
    @classmethod
    def _check_header(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not all(c.isalnum() or c == "-" for c in value):
            raise ValueError(f"not a header name: {value!r}")
        return value.lower()


@dataclass(frozen=True)
class Secrets:
    """The two checked secrets the server needs."""

    password_hash: str
    secret: str


def require_secrets(settings: WebSettings) -> Secrets:
    """The password hash and the session secret, checked; raises :class:`ConfigError` with
    every problem at once. Both are registered as secrets with the log redaction."""
    from thresy.web.security import parse_password_hash

    problems = []
    hash_value = settings.web_password_hash.get_secret_value() if settings.web_password_hash else ""
    secret = settings.web_secret.get_secret_value() if settings.web_secret else ""
    register_secret(hash_value or None, secret or None)
    if not hash_value.strip():
        problems.append(
            "COACH_WEB_PASSWORD_HASH is not set: create a password and its hash with"
            " `coach web password`"
        )
    else:
        try:
            parse_password_hash(hash_value.strip())
        except ValueError as exc:
            problems.append(f"COACH_WEB_PASSWORD_HASH is not usable: {exc}")
    if not secret.strip():
        problems.append("COACH_WEB_SECRET is not set: create one with `openssl rand -hex 32`")
    elif len(secret.strip()) < MIN_SECRET_LENGTH:
        problems.append(
            f"COACH_WEB_SECRET is too short ({len(secret.strip())} characters, at least"
            f" {MIN_SECRET_LENGTH}): create one with `openssl rand -hex 32`"
        )
    if problems:
        raise ConfigError("the web dashboard refuses to start: " + "; ".join(problems))
    return Secrets(password_hash=hash_value.strip(), secret=secret.strip())


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def bind_host(settings: WebSettings, override: str | None = None) -> str:
    """The address to listen on. With ``COACH_WEB_DEV_HTTP`` only a loopback address is
    allowed (127.0.0.1 unless another loopback address is given)."""
    explicit = override or ("web_host" in settings.model_fields_set and settings.web_host)
    if not settings.web_dev_http:
        return override or settings.web_host
    if not explicit:
        return LOOPBACK
    if not is_loopback(explicit):
        raise ConfigError(
            f"COACH_WEB_DEV_HTTP sends the session cookie over plain HTTP, so the server only"
            f" listens on a loopback address, not on {explicit!r}"
        )
    return explicit
