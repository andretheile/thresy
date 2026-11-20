"""`coach sync ...` end to end against the test database and a fake Garmin."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from tests import synthetic
from tests.garmin.offline import GOOD_CODE, OfflineGarmin
from tests.sync.helpers import StaticProvider, activity_at, fit_file, make_config, zipped
from thresy import cli as coach_cli
from thresy import db, migrate
from thresy.config import get_settings
from thresy.garmin.fake import FakeGarminClient
from thresy.sync import agent_requests, state
from thresy.sync import cli as sync_cli
from thresy.sync.poller import POLLER_LOCK

invoke = CliRunner().invoke


def test_help_lists_the_commands() -> None:
    result = invoke(coach_cli.get_app(), ["sync", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("once", "poll", "backfill", "login", "request", "status"):
        assert command in result.output


@pytest.fixture
def env(conn, test_db, tmp_path, monkeypatch):
    """CLI settings: the test database, a fake Garmin, today's runs and wellness."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("COACH_DATABASE_URL", test_db.app_url)
    get_settings.cache_clear()
    config = make_config(tmp_path)
    fake = FakeGarminClient()
    today = datetime.now(UTC).date()
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    fake.add_activity(activity_at(9001, start), original=zipped(fit_file(start)))
    fake.set_wellness(today, sleep=synthetic.sleep(today))
    monkeypatch.setattr(sync_cli, "make_config", lambda: config)
    monkeypatch.setattr(sync_cli, "make_provider", lambda _config: StaticProvider(fake))
    yield {"config": config, "fake": fake, "today": today, "db": test_db}
    get_settings.cache_clear()


def as_agent(monkeypatch, test_db) -> None:
    monkeypatch.setenv("COACH_DATABASE_URL", test_db.agent_url)
    get_settings.cache_clear()


pytestmark = pytest.mark.db


def test_once_runs_a_pass(env, conn) -> None:
    result = invoke(sync_cli.app, ["once", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] and data["result"]["new_activities"] == [9001]
    assert data["result"]["sleep_synced"] is True
    kinds = sorted(r["kind"] for r in conn.execute("SELECT kind FROM events").fetchall())
    assert kinds == ["new_activity", "sleep_arrived"]
    text = invoke(sync_cli.app, ["once"])
    assert text.exit_code == 0 and "activities checked" in text.output


def test_once_is_refused_while_the_poller_runs(env) -> None:
    with db.connect(env["db"].app_url) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (POLLER_LOCK,))
        result = invoke(sync_cli.app, ["once"])
    assert result.exit_code == 1 and "coach sync request" in result.output


def test_once_failure_exit_code(env) -> None:
    env["fake"].fail("get_activities_by_date", RuntimeError("boom"))
    result = invoke(sync_cli.app, ["once", "--json"])
    assert result.exit_code == 1 and json.loads(result.output)["error_kind"] == "error"


def _answer_requests_in_background(env, *, delay: float = 0.3) -> threading.Thread:
    """Play the poller: wait for a pending request, then answer it with one pass."""

    def serve() -> None:
        with db.connect(env["db"].app_url) as conn:
            deadline = time.monotonic() + 10
            while not agent_requests.has_pending(conn) and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(delay)
            agent_requests.sync_and_answer(
                conn,
                env["config"],
                StaticProvider(env["fake"]),
                now=datetime.now(UTC),
                handled_by="test",
            )

    thread = threading.Thread(target=serve)
    thread.start()
    return thread


def test_request_with_the_agent_role_waits_for_the_answer(env, monkeypatch) -> None:
    as_agent(monkeypatch, env["db"])
    thread = _answer_requests_in_background(env)
    result = invoke(sync_cli.app, ["request", "--wait", "10", "--json"])
    thread.join()
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "done" and data["error"] is None
    assert data["result"] == {
        "new_activities": [9001],
        "sleep_synced": True,
        "wake_date": env["today"].isoformat(),
    }

    thread = _answer_requests_in_background(env, delay=0)
    text = invoke(sync_cli.app, ["request", "--wait", "10"])
    thread.join()
    assert text.exit_code == 0, text.output
    assert "sync done: no new activities; sleep of" in text.output and "synced" in text.output


def test_request_without_wait_and_timeout(env, conn, monkeypatch) -> None:
    as_agent(monkeypatch, env["db"])
    queued = invoke(sync_cli.app, ["request"])
    assert queued.exit_code == 0 and "queued" in queued.output
    timed_out = invoke(sync_cli.app, ["request", "--wait", "0.3", "--json"])
    assert timed_out.exit_code == 3
    assert json.loads(timed_out.output)["status"] == "pending"
    assert agent_requests.counts(conn) == {"pending": 2, "running": 0}


def test_request_failure(env, monkeypatch) -> None:
    as_agent(monkeypatch, env["db"])
    env["fake"].fail("get_activities_by_date", RuntimeError("boom"))
    thread = _answer_requests_in_background(env, delay=0)
    result = invoke(sync_cli.app, ["request", "--wait", "10"])
    thread.join()
    assert result.exit_code == 1 and "sync failed: RuntimeError: boom" in result.output


def test_status_as_the_agent(env, conn, monkeypatch) -> None:
    invoke(sync_cli.app, ["once"])
    agent_requests.create_request(conn)
    as_agent(monkeypatch, env["db"])
    result = invoke(sync_cli.app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["last_success_at"] and report["consecutive_failures"] == 0
    assert report["requests"] == {"pending": 1, "running": 0}
    assert report["poller_running"] is False
    assert report["token"]["present"] is None  # no token directory here
    assert report["today"]["sleep_synced_at"] is not None
    text = invoke(sync_cli.app, ["status"])
    assert "last success:" in text.output and "1 pending" in text.output


def test_status_shows_errors_and_login_state(env, conn) -> None:
    state.record_failure(conn, datetime.now(UTC), "auth", "log in please")
    state.merge(conn, state.AUTH, status="mfa_required", reason="code needed")
    env["config"].token_dir.mkdir(parents=True)
    result = invoke(sync_cli.app, ["status"])
    assert "log in please" in result.output and "mfa_required" in result.output
    assert "token:              missing" in result.output


@pytest.fixture
def unmigrated(fresh_db, tmp_path, monkeypatch):
    """An empty database that `coach db migrate` has not been run on."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("COACH_DATABASE_URL", fresh_db.app_url)
    get_settings.cache_clear()
    monkeypatch.setattr(sync_cli, "make_config", lambda: make_config(tmp_path))
    yield fresh_db
    get_settings.cache_clear()


def test_request_and_status_without_the_agent_requests_table(unmigrated, monkeypatch) -> None:
    with db.connect(unmigrated.app_url) as conn:
        migrate.migrate(conn)
        conn.execute("DROP TABLE agent_requests")  # as when its migration was not applied
    expected = (
        'error: database is not migrated (relation "agent_requests" does not exist);'
        " run `coach db migrate`"
    )
    for args in (["status"], ["request"], ["request", "--wait", "5"]):
        result = invoke(sync_cli.app, args)
        assert result.exit_code == 1, result.output
        assert result.output.strip() == expected  # one line, no traceback
        assert isinstance(result.exception, SystemExit)
    as_agent(monkeypatch, unmigrated)  # how the agent runs it
    result = invoke(sync_cli.app, ["request", "--wait", "5", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == expected.removeprefix("error: ")


def test_status_on_an_empty_database(unmigrated) -> None:
    result = invoke(sync_cli.app, ["status"])
    assert result.exit_code == 1
    assert result.output.strip() == (
        'error: database is not migrated (relation "sync_state" does not exist);'
        " run `coach db migrate`"
    )


def test_backfill_command(env, conn) -> None:
    since = (env["today"] - timedelta(days=3)).isoformat()
    result = invoke(sync_cli.app, ["backfill", "--since", since, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["finished"] and data["wellness_days"] == 3
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0
    bad = invoke(sync_cli.app, ["backfill", "--since", "yesterday"])
    assert bad.exit_code == 2


def test_login_code_flow(env, conn, monkeypatch) -> None:
    offline = OfflineGarmin(monkeypatch)
    offline.password_result = "mfa"
    config = make_config(env["config"].data_dir.parent, garmin_email="a@example.invalid",
                         garmin_password="secret")  # fmt: skip
    monkeypatch.setattr(sync_cli, "make_config", lambda: config)
    waiting = invoke(sync_cli.app, ["login"])
    assert waiting.exit_code == 3 and "--code" in waiting.output
    again = invoke(sync_cli.app, ["login", "--json"])
    assert again.exit_code == 3 and json.loads(again.output)["status"] == "mfa_required"
    wrong = invoke(sync_cli.app, ["login", "--code", "000000"])
    assert wrong.exit_code == 1 and "Garmin login failed" in wrong.output
    done = invoke(sync_cli.app, ["login", "--code", GOOD_CODE])
    assert done.exit_code == 0 and "Logged in" in done.output
    assert state.get(conn, state.AUTH)["status"] == "ok"
    token = invoke(sync_cli.app, ["login"])
    assert token.exit_code == 0 and "works" in token.output
    assert offline.count("password_login") == 1
