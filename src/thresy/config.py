"""Settings, read from environment variables prefixed ``COACH_`` and an optional ``.env`` file.

Usage::

    from thresy.config import get_settings

    settings = get_settings()
    settings.database_url

Packages that need settings of their own should not edit this module. They define a small
settings class that reuses the same configuration::

    from pydantic_settings import BaseSettings
    from thresy.config import SETTINGS_CONFIG

    class WeatherSettings(BaseSettings):
        model_config = SETTINGS_CONFIG
        weather_api_key: str | None = None   # read from COACH_WEATHER_API_KEY

Unknown ``COACH_*`` variables (in the environment or in ``.env``) are ignored, so each package
can read its own without the others failing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SETTINGS_CONFIG = SettingsConfigDict(
    env_prefix="COACH_",
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",  # other packages' COACH_* keys
    env_ignore_empty=True,  # `COACH_X=` (as copied from .env.example) means "not set"
)


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


class Settings(BaseSettings):
    """Deployment settings. Every field maps to ``COACH_<FIELD_NAME_IN_UPPER_CASE>``."""

    model_config = SETTINGS_CONFIG

    database_url: str | None = None
    """Read/write connection URL (role ``coach_app``), e.g. ``postgresql://coach_app:...@db/thresy``."""

    database_url_ro: str | None = None
    """Read-only connection URL (role ``coach_ro``), used by ``coach sql``."""

    agent_database_url: str | None = None
    """URL of the ``coach_agent`` role. The bridge hands it to the agent as the agent's own
    ``COACH_DATABASE_URL``; services never connect with it themselves."""

    data_dir: Path = Path("private/data")
    """FIT files, plots and other runtime files. ``activities.fit_path`` is relative to this."""

    workspace_dir: Path = Path("private/workspace")
    """The coach workspace the agent runs in."""

    tz: str = "UTC"
    """The athlete's IANA time zone, e.g. ``America/Chicago``."""

    language: str = "en"
    """Language the coach speaks (an ISO 639-1 code such as ``en`` or ``de``)."""

    name: str = "Thresy"
    """The name the coach uses for itself (``COACH_NAME``)."""

    runner: str = "claude"
    """Coding agent CLI: ``claude``, ``codex``, ``cursor`` or ``fake``. Everything else about
    the runners (``COACH_<NAME>_MODEL_FAST`` etc.) is read by :mod:`thresy.runner.config`."""

    telegram_token: SecretStr | None = None
    telegram_chat_ids: Annotated[tuple[str, ...], NoDecode] = ()
    """Allow-listed chat ids, comma separated in the environment (``123,-456``)."""

    garmin_email: str | None = None
    garmin_password: SecretStr | None = None
    garmin_token_dir: Path = Path("private/garmin")
    """Where the Garmin session tokens are kept (a volume of the sync service only)."""

    legacy_dir: Path | None = None
    """The old bot's export (``COACH_LEGACY_DIR``); used by the importer and parity tests."""

    @field_validator("telegram_chat_ids", mode="before")
    @classmethod
    def _split_chat_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, int):
            return (str(value),)
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @property
    def zone(self) -> ZoneInfo:
        """``tz`` as a :class:`zoneinfo.ZoneInfo`."""
        return ZoneInfo(self.tz)

    def require_database_url(self, *, read_only: bool = False) -> str:
        """Return the read/write (or read-only) URL, or raise :class:`ConfigError` if unset."""
        url = self.database_url_ro if read_only else self.database_url
        if not url:
            name = "COACH_DATABASE_URL_RO" if read_only else "COACH_DATABASE_URL"
            raise ConfigError(f"{name} is not set (see .env.example)")
        return url

    def require_agent_database_url(self) -> str:
        """Return the ``coach_agent`` URL, or raise :class:`ConfigError` if unset."""
        if not self.agent_database_url:
            raise ConfigError("COACH_AGENT_DATABASE_URL is not set (see .env.example)")
        return self.agent_database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``get_settings.cache_clear()`` in tests)."""
    return Settings()
