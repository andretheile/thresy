"""Settings of the sync service.

The Garmin login (``COACH_GARMIN_EMAIL``, ``COACH_GARMIN_PASSWORD``, ``COACH_GARMIN_TOKEN_DIR``),
``COACH_TZ`` and ``COACH_DATA_DIR`` come from the core :class:`thresy.config.Settings`. The
sync's own knobs are ``COACH_SYNC_*`` variables (:class:`SyncSettings`). Code takes a
:class:`SyncConfig`, which combines both; tests build one directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings

from thresy.config import SETTINGS_CONFIG, Settings, get_settings


class SyncSettings(BaseSettings):
    """``COACH_SYNC_*`` environment variables (all optional)."""

    model_config = SETTINGS_CONFIG

    sync_lookback_days: int = Field(7, ge=1)
    """Activities started in the last N local days are listed on every pass."""
    sync_new_activity_max_age_h: float = Field(48, gt=0)
    """``new_activity`` events only for activities that started at most this long ago."""
    sync_interval_s: float = Field(1200, gt=0)
    """Normal time between passes."""
    sync_jitter_s: float = Field(120, ge=0)
    """Each interval is lengthened or shortened by a random amount up to this."""
    sync_morning_start: time = time(5, 0)
    sync_morning_end: time = time(11, 0)
    """Local time window in which the poller looks more often while last night's sleep has
    not arrived yet."""
    sync_morning_interval_s: float = Field(300, gt=0)
    sync_request_check_s: float = Field(5, gt=0)
    """How often the poller looks for ``sync_now`` requests between passes."""
    sync_recheck_days: int = Field(3, ge=0)
    """Days before yesterday that are fetched again while still incomplete."""
    sync_recheck_interval_s: float = Field(3 * 3600, gt=0)
    """At most this often per incomplete older day."""
    sync_steps_final_after_h: float = Field(12, ge=0)
    """A day's step count counts as final when it was fetched this long after the day ended."""
    sync_pr_refresh_h: float = Field(24, gt=0)
    """Personal records are refreshed this often, and after every new run."""
    sync_fit_max_attempts: int = Field(3, ge=1)
    """Downloads of an activity's original file before giving up on it."""
    sync_login_retry_h: float = Field(24, gt=0)
    """After a failed or unfinished password login, no automatic password login for this long
    (``coach sync login`` by a human is always allowed)."""
    sync_rate_limit_backoff_s: float = Field(300, gt=0)
    sync_rate_limit_backoff_max_s: float = Field(6 * 3600, gt=0)
    """After HTTP 429 the next pass waits ``backoff``, doubling on each further 429 up to the
    maximum; a successful pass resets it."""
    sync_fail_event_after: int = Field(3, ge=1)
    """Raise ``sync_failed`` after this many failed passes in a row (auth problems have their
    own event)."""
    sync_backfill_pause_s: float = Field(2.0, ge=0)
    """Pause before every Garmin request of ``coach sync backfill``."""


@dataclass(frozen=True)
class SyncConfig:
    """Everything the sync needs. Build it with :meth:`load` (environment) or directly."""

    zone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    data_dir: Path = Path("private/data")
    token_dir: Path = Path("private/garmin")
    garmin_email: str | None = None
    garmin_password: str | None = field(default=None, repr=False)

    lookback_days: int = 7
    new_activity_max_age_h: float = 48
    interval_s: float = 1200
    jitter_s: float = 120
    morning_start: time = time(5, 0)
    morning_end: time = time(11, 0)
    morning_interval_s: float = 300
    request_check_s: float = 5
    recheck_days: int = 3
    recheck_interval_s: float = 3 * 3600
    steps_final_after_h: float = 12
    pr_refresh_h: float = 24
    fit_max_attempts: int = 3
    login_retry_h: float = 24
    rate_limit_backoff_s: float = 300
    rate_limit_backoff_max_s: float = 6 * 3600
    fail_event_after: int = 3
    backfill_pause_s: float = 2.0
    backfill_chunk_days: int = 28
    backfill_max_rate_limits: int = 5
    lock_retry_s: float = 30
    """While another poller holds the lock, try again this often."""
    db_retry_s: float = 30
    """After losing the database connection, reconnect after this long."""

    @classmethod
    def load(cls, settings: Settings | None = None, sync: SyncSettings | None = None) -> SyncConfig:
        core = settings or get_settings()
        s = sync or SyncSettings()
        password = core.garmin_password.get_secret_value() if core.garmin_password else None
        return cls(
            zone=core.zone,
            data_dir=core.data_dir,
            token_dir=core.garmin_token_dir,
            garmin_email=core.garmin_email or None,
            garmin_password=password or None,
            lookback_days=s.sync_lookback_days,
            new_activity_max_age_h=s.sync_new_activity_max_age_h,
            interval_s=s.sync_interval_s,
            jitter_s=s.sync_jitter_s,
            morning_start=s.sync_morning_start,
            morning_end=s.sync_morning_end,
            morning_interval_s=s.sync_morning_interval_s,
            request_check_s=s.sync_request_check_s,
            recheck_days=s.sync_recheck_days,
            recheck_interval_s=s.sync_recheck_interval_s,
            steps_final_after_h=s.sync_steps_final_after_h,
            pr_refresh_h=s.sync_pr_refresh_h,
            fit_max_attempts=s.sync_fit_max_attempts,
            login_retry_h=s.sync_login_retry_h,
            rate_limit_backoff_s=s.sync_rate_limit_backoff_s,
            rate_limit_backoff_max_s=s.sync_rate_limit_backoff_max_s,
            fail_event_after=s.sync_fail_event_after,
            backfill_pause_s=s.sync_backfill_pause_s,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.garmin_email and self.garmin_password)
