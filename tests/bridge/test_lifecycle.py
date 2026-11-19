"""Shutdown, restart recovery and the settings checks of `coach bridge run`."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from thresy.bridge.main import build_transport, check_settings
from thresy.bridge.settings import BridgeSettings, QuietHours
from thresy.config import ConfigError
from thresy.runner import FakeRunner, Progress
from thresy.transport import InboundMessage

from .conftest import CHAT, START, GatedRunner, local, make_settings

pytestmark = pytest.mark.db


def test_shutdown_cancels_the_running_agent_and_tells_the_athlete(make_harness) -> None:
    async def scenario() -> None:
        runner = GatedRunner(lambda req: [Progress("text", "never")])
        async with make_harness(runner) as h:
            await h.say("a long question")
            await asyncio.wait_for(runner.started.wait(), 5)
            await h.bridge.shutdown()
            run = h.one("SELECT status, error_kind, error_detail FROM agent_runs")
            assert run == {
                "status": "failed",
                "error_kind": "cancelled",
                "error_detail": "shutdown",
            }
            assert h.transport.texts == [
                "I was restarted while I was working on your last message, so it has no answer."
                " Please send it again."
            ]
            assert h.transport.stopped
            assert h.transport.busy_log[-1] == (CHAT, False)
            # Messages arriving while stopping are logged, not run.
            await h.bridge.on_message(
                InboundMessage(transport="fake", chat_id=CHAT, kind="text", text="anyone there?")
            )
            assert len(runner.requests) == 1
            row = h.one("SELECT run_id FROM messages WHERE text = 'anyone there?'")
            assert row["run_id"] is None

    asyncio.run(scenario())


def test_restart_recovers_interrupted_runs_and_unanswered_messages(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(lambda req: [Progress("text", "answered after restart")])
        async with make_harness(runner, start=False) as h:
            session = uuid.uuid4()
            h.conn.execute(
                "INSERT INTO agent_sessions (id, chat_id, runner, day) VALUES (%s, %s, 'fake', %s)",
                (session, CHAT, START.date()),
            )
            user_run = h.one(
                "INSERT INTO agent_runs (session_id, trigger, runner, status)"
                " VALUES (%s, 'user_message', 'fake', 'running') RETURNING id",
                (session,),
            )["id"]
            briefing_run = h.one(
                "INSERT INTO agent_runs (session_id, trigger, runner, status)"
                " VALUES (%s, 'morning_briefing', 'fake', 'running') RETURNING id",
                (session,),
            )["id"]
            insert = (
                "INSERT INTO messages (ts, chat_id, direction, kind, text, transport, run_id)"
                " VALUES (%s, %s, 'in', 'text', %s, 'fake', %s)"
            )
            h.conn.execute(
                insert, (START - timedelta(minutes=5), CHAT, "in the crashed run", user_run)
            )
            h.conn.execute(
                insert, (START - timedelta(minutes=2), CHAT, "never reached a run", None)
            )
            h.conn.execute(
                insert, (START - timedelta(hours=3), CHAT, "too old to answer now", None)
            )
            h.conn.execute(insert, (START - timedelta(minutes=1), "999", "foreign chat", None))
            await h.bridge.start(background=False)
            await h.settle()
            runs = h.rows("SELECT id, status, error_kind FROM agent_runs ORDER BY id")
            assert [(r["status"], r["error_kind"]) for r in runs[:2]] == [
                ("failed", "interrupted"),
                ("failed", "interrupted"),
            ]
            assert h.transport.texts[:2] == [
                "I was restarted while I was working on your last message, so it has no answer."
                " Please send it again.",
                "Morning briefing: It was interrupted by a restart of the bridge.",
            ]
            assert h.transport.texts[2:] == ["answered after restart"]
            assert len(runner.requests) == 1
            # Only the message that never reached a run is answered; older ones are context.
            asked = runner.requests[0].prompt.split("(end of earlier messages)")[-1]
            assert "never reached a run" in asked
            assert "too old to answer now" not in asked and "in the crashed run" not in asked
            assert user_run in [r["id"] for r in runs] and briefing_run in [r["id"] for r in runs]

    asyncio.run(scenario())


def test_settings_checks(bridge_db, tmp_path) -> None:
    good = make_settings(bridge_db, tmp_path)
    check_settings(good)
    same = make_settings(bridge_db, tmp_path, agent_database_url=bridge_db.app_url)
    with pytest.raises(ConfigError, match="must be the coach_agent URL"):
        check_settings(same)
    bs = BridgeSettings(_env_file=None)
    transport = build_transport(good, bs)
    assert transport.name == "telegram" and transport.default_chat_id == CHAT
    assert transport.inbox_dir == (tmp_path / "workspace" / "inbox").resolve()
    with pytest.raises(ConfigError, match="COACH_TELEGRAM_TOKEN"):
        build_transport(make_settings(bridge_db, tmp_path, telegram_token=None), bs)
    with pytest.raises(ConfigError, match="COACH_TELEGRAM_CHAT_IDS"):
        build_transport(make_settings(bridge_db, tmp_path, telegram_chat_ids=""), bs)
    with pytest.raises(ConfigError, match="only 'telegram'"):
        build_transport(good, BridgeSettings(_env_file=None, bridge_transport="web"))


def test_quiet_hours_parsing() -> None:
    quiet = QuietHours.parse("22:00-06:30")
    assert quiet is not None
    assert quiet.contains(local(2030, 1, 2, 23, 0).time().replace(tzinfo=None))
    assert quiet.contains(local(2030, 1, 2, 6, 29).time().replace(tzinfo=None))
    assert not quiet.contains(local(2030, 1, 2, 6, 30).time().replace(tzinfo=None))
    assert quiet.end_after(local(2030, 1, 2, 23, 0)) == local(2030, 1, 3, 6, 30)
    day = QuietHours.parse("13:00-14:00")
    assert day is not None and day.contains(local(2030, 1, 2, 13, 30).time())
    assert QuietHours.parse("off") is None and QuietHours.parse("") is None
    with pytest.raises(ValueError):
        QuietHours.parse("22-06")
    with pytest.raises(ValueError):
        BridgeSettings(_env_file=None, bridge_quiet_hours="25:00-06:00")
    with pytest.raises(ValueError):
        BridgeSettings(_env_file=None, bridge_briefing_fallback="10h")
