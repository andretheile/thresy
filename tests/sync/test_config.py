from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from thresy.config import Settings
from thresy.sync.config import SyncConfig, SyncSettings


def test_defaults_and_core_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    for name in ("COACH_GARMIN_EMAIL", "COACH_GARMIN_PASSWORD", "COACH_SYNC_INTERVAL_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COACH_TZ", "America/Chicago")
    monkeypatch.setenv("COACH_GARMIN_TOKEN_DIR", "/garmin")
    config = SyncConfig.load(Settings(), SyncSettings())
    assert config.zone.key == "America/Chicago" and config.token_dir == Path("/garmin")
    assert config.lookback_days == 7 and config.interval_s == 1200
    assert config.new_activity_max_age_h == 48
    assert (config.morning_start, config.morning_end) == (time(5), time(11))
    assert not config.has_credentials and config.garmin_password is None


def test_sync_variables(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COACH_GARMIN_EMAIL", "athlete@example.invalid")
    monkeypatch.setenv("COACH_GARMIN_PASSWORD", "secret")
    monkeypatch.setenv("COACH_SYNC_INTERVAL_S", "900")
    monkeypatch.setenv("COACH_SYNC_MORNING_START", "04:30")
    monkeypatch.setenv("COACH_SYNC_LOOKBACK_DAYS", "14")
    monkeypatch.setenv("COACH_SYNC_BACKFILL_PAUSE_S", "")  # empty means unset
    config = SyncConfig.load(Settings(), SyncSettings())
    assert config.has_credentials and config.garmin_password == "secret"
    assert "secret" not in repr(config)
    assert config.interval_s == 900 and config.morning_start == time(4, 30)
    assert config.lookback_days == 14 and config.backfill_pause_s == 2.0


def test_invalid_values_are_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COACH_SYNC_LOOKBACK_DAYS", "0")
    with pytest.raises(ValueError, match="lookback"):
        SyncSettings()
