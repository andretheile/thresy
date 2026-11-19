from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from thresy.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("COACH_"):
            monkeypatch.delenv(key)


def test_defaults_are_generic() -> None:
    s = Settings(_env_file=None)
    assert s.tz == "UTC"
    assert s.language == "en"
    assert s.name == "Thresy"
    assert s.runner == "claude"
    assert s.database_url is None and s.database_url_ro is None
    assert s.telegram_chat_ids == ()
    assert s.legacy_dir is None
    assert s.data_dir == Path("private/data")


def test_reads_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("COACH_TZ", "America/Chicago")
    monkeypatch.setenv("COACH_LANGUAGE", "de")
    monkeypatch.setenv("COACH_NAME", "Coachy")
    monkeypatch.setenv("COACH_TELEGRAM_CHAT_IDS", "123, -456,")
    monkeypatch.setenv("COACH_TELEGRAM_TOKEN", "secret")
    monkeypatch.setenv("COACH_LEGACY_DIR", "/tmp/legacy")
    monkeypatch.setenv("COACH_RUNNER_TIMEOUT_S", "60")
    s = Settings(_env_file=None)
    assert s.zone.key == "America/Chicago"
    assert (s.language, s.name) == ("de", "Coachy")
    assert s.telegram_chat_ids == ("123", "-456")
    assert s.telegram_token is not None and s.telegram_token.get_secret_value() == "secret"
    assert "secret" not in repr(s)
    assert s.legacy_dir == Path("/tmp/legacy")
    assert s.runner_timeout_s == 60


def test_reads_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("COACH_DATABASE_URL=postgresql://u@h/db\nUNRELATED=1\nDB_APP_PASSWORD=x\n")
    s = Settings(_env_file=env)
    assert s.require_database_url() == "postgresql://u@h/db"
    with pytest.raises(ConfigError, match="COACH_DATABASE_URL_RO"):
        s.require_database_url(read_only=True)


def test_rejects_unknown_time_zone(monkeypatch) -> None:
    monkeypatch.setenv("COACH_TZ", "Mars/Olympus")
    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings(_env_file=None)
