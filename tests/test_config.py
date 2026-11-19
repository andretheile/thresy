from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from thresy.config import ConfigError, Settings

RUNNER_KEYS = (
    "COACH_CLAUDE_BIN",
    "COACH_CLAUDE_MODEL_FAST",
    "COACH_CODEX_PERMISSION_ARGS",
    "COACH_CURSOR_ENV",
    "COACH_RUNNER_TIMEOUT_S",
)


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
    assert s.agent_database_url is None
    assert s.telegram_chat_ids == ()
    assert s.legacy_dir is None
    assert s.data_dir == Path("private/data")
    assert s.garmin_token_dir == Path("private/garmin")
    assert s.garmin_email is None and s.garmin_password is None


def test_runner_settings_live_in_the_runner_package() -> None:
    fields = set(Settings.model_fields)
    assert not {f for f in fields if f.startswith("runner_")}
    assert "runner" in fields


def test_reads_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("COACH_TZ", "America/Chicago")
    monkeypatch.setenv("COACH_LANGUAGE", "de")
    monkeypatch.setenv("COACH_NAME", "Coachy")
    monkeypatch.setenv("COACH_TELEGRAM_CHAT_IDS", "123, -456,")
    monkeypatch.setenv("COACH_TELEGRAM_TOKEN", "secret")
    monkeypatch.setenv("COACH_LEGACY_DIR", "/tmp/legacy")
    monkeypatch.setenv("COACH_AGENT_DATABASE_URL", "postgresql://coach_agent@h/db")
    monkeypatch.setenv("COACH_GARMIN_EMAIL", "athlete@example.org")
    monkeypatch.setenv("COACH_GARMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("COACH_GARMIN_TOKEN_DIR", "/garmin")
    s = Settings(_env_file=None)
    assert s.zone.key == "America/Chicago"
    assert (s.language, s.name) == ("de", "Coachy")
    assert s.telegram_chat_ids == ("123", "-456")
    assert s.telegram_token is not None and s.telegram_token.get_secret_value() == "secret"
    assert s.legacy_dir == Path("/tmp/legacy")
    assert s.require_agent_database_url() == "postgresql://coach_agent@h/db"
    assert s.garmin_email == "athlete@example.org"
    assert s.garmin_password is not None and s.garmin_password.get_secret_value() == "hunter2"
    assert s.garmin_token_dir == Path("/garmin")
    assert "secret" not in repr(s) and "hunter2" not in repr(s)


def test_reads_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("COACH_DATABASE_URL=postgresql://u@h/db\nUNRELATED=1\nDB_APP_PASSWORD=x\n")
    s = Settings(_env_file=env)
    assert s.require_database_url() == "postgresql://u@h/db"
    with pytest.raises(ConfigError, match="COACH_DATABASE_URL_RO"):
        s.require_database_url(read_only=True)
    with pytest.raises(ConfigError, match="COACH_AGENT_DATABASE_URL"):
        s.require_agent_database_url()


def test_unknown_coach_keys_are_tolerated(tmp_path: Path, monkeypatch) -> None:
    """Other packages (the runner, later ones) read COACH_* keys this class does not know."""
    env = tmp_path / ".env"
    env.write_text("".join(f"{key}=x\n" for key in RUNNER_KEYS) + "COACH_LANGUAGE=fr\n")
    monkeypatch.setenv("COACH_SOMETHING_NEW", "1")
    s = Settings(_env_file=env)
    assert s.language == "fr"
    assert not hasattr(s, "claude_bin")


def test_empty_values_count_as_unset(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("COACH_GARMIN_EMAIL=\nCOACH_TELEGRAM_TOKEN=\nCOACH_TZ=\n")
    monkeypatch.setenv("COACH_AGENT_DATABASE_URL", "")
    s = Settings(_env_file=env)
    assert s.garmin_email is None and s.telegram_token is None
    assert s.tz == "UTC" and s.agent_database_url is None


def test_example_env_file_loads(monkeypatch) -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    s = Settings(_env_file=example)
    assert s.require_database_url().startswith("postgresql://coach_app:")
    assert s.require_database_url(read_only=True).startswith("postgresql://coach_ro:")
    assert s.require_agent_database_url().startswith("postgresql://coach_agent:")
    assert s.garmin_email is None and s.telegram_token is None


def test_rejects_unknown_time_zone(monkeypatch) -> None:
    monkeypatch.setenv("COACH_TZ", "Mars/Olympus")
    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings(_env_file=None)
