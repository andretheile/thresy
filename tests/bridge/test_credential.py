"""Notices before the agent's model credential expires, and /status (fake runner, fake clock)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from thresy.bridge.settings import BridgeSettings, parse_expiry
from thresy.runner import FakeRunner

from .conftest import ZONE, local

pytestmark = pytest.mark.db

EXPIRES = local(2030, 2, 1, 8, 0)  # 30 days after the harness's start
NO_BRIEFING = {"bridge_briefing": False}


def notices(h) -> list[str]:
    return [text for text in h.transport.texts if "expires on" in text or "läuft am" in text]


def remembered(h) -> object:
    row = h.conn.execute("SELECT value FROM sync_state WHERE key = 'bridge:credential'").fetchone()
    return None if row is None else row["value"]


async def status(h) -> str:
    await h.say("/status")
    await h.settle()
    return h.transport.texts[-1]


def test_notices_come_at_a_weekly_cadence_and_once_more_before_the_end(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        async with make_harness(runner, bridge=NO_BRIEFING) as h:
            await h.say("hi")
            await h.settle()
            assert h.transport.texts == ["OK"]
            assert remembered(h)["expires_at"] == EXPIRES.astimezone(UTC).isoformat()

            await h.tick()
            assert notices(h) == []  # 30 days left

            sent = []
            for days_left in (21, 20, 18, 15, 14, 10, 7, 5, 3, 2, 1.5, 1, 0.1, -1):
                h.clock.set(EXPIRES - timedelta(days=days_left))
                await h.tick()
                await h.tick()  # a second tick at the same moment sends nothing more
                if len(notices(h)) > len(sent):
                    sent.append(days_left)
            assert sent == [21, 14, 7, 2]
            texts = notices(h)
            assert texts[0] == (
                "The login of the coach's AI agent expires on 2030-02-01 (in 21 days). After"
                " that the coach cannot answer until the owner renews it (docs/deploy.md,"
                ' section "The model credential").'
            )
            assert "(in 2 days)" in texts[-1]
            assert all("COACH_BRIDGE_CREDENTIAL_EXPIRES" not in text for text in texts)
            assert len(runner.requests) == 1  # notices need no agent run

    asyncio.run(scenario())


def test_a_restart_repeats_no_notice(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        async with make_harness(runner, bridge=NO_BRIEFING) as h:
            await h.say("hi")
            await h.settle()
            h.clock.set(EXPIRES - timedelta(days=20))
            await h.tick()
            assert len(notices(h)) == 1
            clock = h.clock
        async with make_harness(FakeRunner(), clock=clock, bridge=NO_BRIEFING) as h:
            await h.tick()
            assert notices(h) == []  # the date and the last notice were kept
            h.clock.advance(days=7)
            await h.tick()
            assert len(notices(h)) == 1

    asyncio.run(scenario())


def test_a_due_notice_waits_for_the_end_of_quiet_hours(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        bridge = {**NO_BRIEFING, "bridge_quiet_hours": "22:00-06:00"}
        async with make_harness(runner, bridge=bridge) as h:
            await h.say("hi")
            await h.settle()
            h.clock.set(local(2030, 1, 11, 23, 0))
            await h.tick()
            assert notices(h) == []
            h.clock.set(local(2030, 1, 12, 6, 1))
            await h.tick()
            assert len(notices(h)) == 1 and "(in 20 days)" in notices(h)[0]

    asyncio.run(scenario())


def test_the_setting_counts_when_the_runner_reports_nothing(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK")  # like a token from `claude setup-token`
        bridge = {**NO_BRIEFING, "bridge_credential_expires": "2030-02-01"}
        async with make_harness(runner, bridge=bridge) as h:
            await h.say("hi")
            await h.settle()
            assert remembered(h) is None
            assert "Agent login valid until: 2030-02-01 (in 30 days)" in await status(h)

            h.clock.set(local(2030, 1, 11, 8, 0))
            await h.tick()
            assert len(notices(h)) == 1
            assert "expires on 2030-02-01 (in 21 days)" in notices(h)[0]
            assert notices(h)[0].endswith(
                "The date comes from COACH_BRIDGE_CREDENTIAL_EXPIRES: after renewing, set it to"
                " the new date."
            )

            # A date reported with a run wins; a successful run without one drops it again.
            runner.credential_expires_at = local(2031, 1, 1, 12, 0)
            await h.say("hi")
            await h.settle()
            assert "Agent login valid until: 2031-01-01 (in 355 days)" in await status(h)
            runner.credential_expires_at = None
            await h.say("hi")
            await h.settle()
            assert remembered(h)["expires_at"] is None
            assert "Agent login valid until: 2030-02-01 (in 21 days)" in await status(h)

    asyncio.run(scenario())


def test_a_failed_run_keeps_the_known_date(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        async with make_harness(runner, bridge=NO_BRIEFING) as h:
            await h.say("hi")
            await h.settle()
            runner.credential_expires_at = None
            runner.fail = "auth"
            await h.say("hi again")
            await h.settle()
            assert remembered(h)["expires_at"] == EXPIRES.astimezone(UTC).isoformat()

    asyncio.run(scenario())


def test_status_without_a_known_date_and_in_german(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(bridge=NO_BRIEFING) as h:
            assert "Agent login valid until: unknown" in await status(h)
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        async with make_harness(runner, settings={"language": "de"}, bridge=NO_BRIEFING) as h:
            await h.say("hallo")
            await h.settle()
            assert "Anmeldung des Agenten gültig bis: 2030-02-01 (in 30 Tagen)" in await status(h)
            h.clock.set(EXPIRES - timedelta(days=2))
            await h.tick()
            assert notices(h) == [
                "Die Anmeldung des KI-Agenten des Coaches läuft am 2030-02-01 ab (in 2 Tagen)."
                " Danach kann der Coach nicht mehr antworten, bis der Betreiber sie erneuert"
                ' (docs/deploy.md, Abschnitt "The model credential").'
            ]
            h.clock.set(EXPIRES - timedelta(hours=1))
            assert "gültig bis: 2030-02-01 (heute)" in await status(h)
            h.clock.set(EXPIRES + timedelta(hours=1))
            assert "gültig bis: abgelaufen am 2030-02-01" in await status(h)

    asyncio.run(scenario())


def test_notices_can_be_turned_off(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(reply="OK", credential_expires_at=EXPIRES)
        bridge = {**NO_BRIEFING, "bridge_credential_warn_days": 0}
        async with make_harness(runner, bridge=bridge) as h:
            await h.say("hi")
            await h.settle()
            h.clock.set(EXPIRES - timedelta(days=1))
            await h.tick()
            assert notices(h) == []

    asyncio.run(scenario())


def test_expiry_setting_is_checked_at_start_up() -> None:
    def settings(value: str) -> BridgeSettings:
        return BridgeSettings(_env_file=None, bridge_credential_expires=value)

    assert settings(" 2030-02-01 ").bridge_credential_expires == "2030-02-01"
    with pytest.raises(ValidationError, match="expected a date"):
        settings("next year")
    assert parse_expiry("2030-02-01", ZONE) == datetime(2030, 2, 1, tzinfo=ZONE)
    assert parse_expiry("2030-02-01T12:00:00Z") == datetime(2030, 2, 1, 12, tzinfo=UTC)
    assert parse_expiry("2030-02-01T12:00", ZONE) == datetime(2030, 2, 1, 12, tzinfo=ZONE)
