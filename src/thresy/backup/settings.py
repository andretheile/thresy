"""Settings of the backup (``COACH_BACKUP_*``), on top of the core settings in thresy.config.

The database (``COACH_DATABASE_URL``, role ``coach_app``), the FIT files (``COACH_DATA_DIR``),
the workspace (``COACH_WORKSPACE_DIR``) and the time zone (``COACH_TZ``) come from the core
settings.
"""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG


class BackupSettings(BaseSettings):
    """``COACH_BACKUP_*`` environment variables."""

    model_config = SETTINGS_CONFIG

    backup_url: str | None = None
    """Where backups go: ``file:///path`` or ``gs://bucket/prefix``."""
    backup_age_recipient: str | None = None
    """age public key(s) (``age1...``; several separated by commas or new lines). When set,
    everything is encrypted before it leaves the host; restoring needs the private key."""
    backup_age_bin: str = "age"
    backup_gcs_credentials: Path | None = None
    """Service-account key (JSON) for ``gs://`` targets. Unset: Application Default
    Credentials. An empty file counts as unset."""
    backup_pg_bin_dir: Path | None = None
    """Directory with ``pg_dump`` and ``pg_restore``. Unset: ``/usr/lib/postgresql/<server
    major version>/bin`` if it exists, else ``PATH``. Their major version must equal the
    server's."""
    backup_work_dir: Path | None = None
    """Where dumps and encrypted copies are written before the upload (default: the system's
    temporary directory). Needs about twice the size of a dump."""
    backup_at: str = "03:30"
    """Local time (``COACH_TZ``) of the scheduled backup (``coach backup schedule``)."""
    backup_weekdays: str = "daily"
    """Days of the scheduled backup: ``daily``, or weekday names such as ``sun`` or
    ``mon,thu`` (English, three letters or in full, any case)."""
    backup_restore_check_days: int = Field(7, ge=0)
    """``coach backup schedule`` also restores a run's dump into a scratch database when the
    last restore check is this many days old (0: never). It can only happen during a backup,
    so with fewer days than between two backups every backup includes one."""

    @field_validator("backup_at")
    @classmethod
    def _check_at(cls, value: str) -> str:
        parse_time(value)
        return value

    @field_validator("backup_weekdays")
    @classmethod
    def _check_weekdays(cls, value: str) -> str:
        parse_weekdays(value)
        return value

    @property
    def recipients(self) -> list[str]:
        return [r.strip() for r in re.split(r"[,\n]", self.backup_age_recipient or "") if r.strip()]

    @property
    def at(self) -> time:
        return parse_time(self.backup_at)

    @property
    def weekdays(self) -> frozenset[int]:
        """Weekday numbers of the scheduled backup (Monday 0, as ``date.weekday()``)."""
        return parse_weekdays(self.backup_weekdays)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FULL_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_weekdays(value: str) -> frozenset[int]:
    """``daily`` or weekday names separated by commas or spaces -> weekday numbers."""
    days: set[int] = set()
    for word in re.split(r"[,\s]+", value.strip().lower()):
        if not word:
            continue
        if word == "daily":
            days.update(range(7))
        elif word in WEEKDAYS:
            days.add(WEEKDAYS.index(word))
        elif word in _FULL_NAMES:
            days.add(_FULL_NAMES.index(word))
        else:
            raise ValueError(
                f"unknown weekday {word!r} in {value!r}: expected daily or weekday names such as "
                f"sun or mon,thu ({', '.join(WEEKDAYS)})"
            )
    if not days:
        raise ValueError("no weekday given: expected daily or weekday names such as sun")
    return frozenset(days)


def describe_weekdays(days: frozenset[int]) -> str:
    """``daily`` or the names in week order (``mon, thu``)."""
    if len(days) == 7:
        return "daily"
    return ", ".join(WEEKDAYS[day] for day in sorted(days))


def parse_time(value: str) -> time:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))
