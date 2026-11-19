"""Chat commands answered without an agent run."""

from __future__ import annotations

import asyncio
import json

import pytest

from thresy.bridge.commands import parse_command
from thresy.runner import FakeRunner, Progress

from .conftest import CHAT, GatedRunner

pytestmark = pytest.mark.db


def test_parse_command() -> None:
    assert parse_command("/status") == "/status"
    assert parse_command("/Status@SomeBot please") == "/status"
    assert parse_command("/plan tomorrow") is None  # goes to the agent
    assert parse_command("status") is None
    assert parse_command("") is None


def test_help_and_start(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            await h.say("/help")
            await h.say("/start")
            await h.settle()
            assert h.runner.requests == []
            assert len(h.transport.texts) == 2
            assert all(t.startswith("Commands:\n/status") for t in h.transport.texts)
            kinds = [r["kind"] for r in h.rows("SELECT kind FROM messages WHERE direction = 'in'")]
            assert kinds == ["command", "command"]

    asyncio.run(scenario())


def test_unknown_slash_words_go_to_the_agent(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(FakeRunner(reply="Here is the plan.")) as h:
            await h.say("/plan tomorrow")
            await h.settle()
            assert "/plan tomorrow" in h.runner.requests[0].prompt
            assert h.transport.texts == ["Here is the plan."]

    asyncio.run(scenario())


def test_status_answers_while_a_run_is_in_progress(make_harness) -> None:
    async def scenario() -> None:
        runner = GatedRunner(lambda req: [Progress("text", "done")])
        runner.cost_usd = 0.25
        async with make_harness(runner) as h:
            # As thresy.sync.state records it.
            sync_status = {
                "last_success_at": h.clock.now().isoformat(),
                "consecutive_failures": 2,
                "last_error": "Garmin request failed: 503",
            }
            h.conn.execute(
                "INSERT INTO sync_state (key, value) VALUES ('sync_status', %s),"
                " ('garmin_auth', %s)",
                (json.dumps(sync_status), json.dumps({"status": "login_paused"})),
            )
            await h.say("a question")
            await asyncio.wait_for(runner.started.wait(), 5)
            await h.say("/status")
            await asyncio.wait(list(h.bridge._tasks), timeout=5)
            await h.bridge.outbox.deliver_pending()
            status = h.transport.texts[0]
            assert status.startswith("**Status**")
            assert "Last successful sync: 2030-01-02 08:00 (0 min)" in status
            assert "Newest data: activity none, sleep none" in status
            assert "Sync failing, passes in a row: 2× – Garmin request failed: 503" in status
            assert "Garmin login: login_paused" in status
            assert "Agent (fake): ok – fake runner (no CLI)" in status
            assert "Agent runs today: 1, cost –" in status
            assert "Outbox: 0 waiting, 0 failed today" in status
            assert "Working on a request right now." in status
            runner.gate.set()
            await h.settle()
            await h.say("/status")
            await h.settle()
            assert "Agent runs today: 1, cost $0.25" in h.transport.texts[-1]
            assert "Working on" not in h.transport.texts[-1]

    asyncio.run(scenario())


def test_status_in_quiet_hours_and_german(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(
            settings={"language": "de"}, bridge={"bridge_quiet_hours": "07:00-09:00"}
        ) as h:
            await h.say("/status")
            await h.settle()
            status = h.transport.texts[0]
            assert "Gerade Ruhezeit: Proaktive Nachrichten warten bis 09:00." in status
            assert "Letzte Sync-Aktivität: unbekannt" in status

    asyncio.run(scenario())


async def fake_sync_service(h, *, status: str, result=None, error=None) -> None:
    """Pick up one sync_now request like the sync service would."""
    while True:
        row = h.conn.execute(
            "SELECT id FROM agent_requests WHERE status = 'pending' AND kind = 'sync_now'"
        ).fetchone()
        if row:
            h.conn.execute(
                "UPDATE agent_requests SET status = %s, result = %s, error = %s,"
                " handled_by = 'test' WHERE id = %s",
                (status, json.dumps(result) if result is not None else None, error, row["id"]),
            )
            return
        await asyncio.sleep(0.01)


def test_sync_command_reports_the_result(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            service = asyncio.create_task(
                fake_sync_service(
                    h,
                    status="done",
                    result={
                        "new_activities": [1, 2],
                        "sleep_synced": True,
                        "wake_date": "2030-01-02",
                    },
                )
            )
            await h.say("/sync")
            await h.settle()
            await service
            assert h.transport.texts == ["Sync done: 2 new activities; last night's sleep: synced."]
            assert h.transport.busy_log == [(CHAT, True), (CHAT, False)]
            assert h.runner.requests == []

    asyncio.run(scenario())


def test_sync_command_reports_a_failure(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            service = asyncio.create_task(
                fake_sync_service(h, status="failed", error="Garmin login required")
            )
            await h.say("/sync")
            await h.settle()
            await service
            assert h.transport.texts == ["Sync failed: Garmin login required"]

    asyncio.run(scenario())


def test_sync_command_times_out(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(bridge={"bridge_sync_request_timeout_s": 120}) as h:
            await h.say("/sync")
            await asyncio.sleep(0.05)
            assert h.transport.texts == []
            h.clock.advance(121)
            await h.settle()
            assert h.transport.texts == [
                "The sync service has not answered within 2 minutes. It may not be running."
            ]
            assert h.one("SELECT status FROM agent_requests")["status"] == "pending"

    asyncio.run(scenario())


def test_sync_command_without_the_requests_table(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            h.conn.execute("ALTER TABLE agent_requests RENAME TO agent_requests_hidden")
            try:
                await h.say("/sync")
                await h.settle()
            finally:
                h.conn.execute("ALTER TABLE agent_requests_hidden RENAME TO agent_requests")
            assert "not available yet" in h.transport.texts[0]

    asyncio.run(scenario())
