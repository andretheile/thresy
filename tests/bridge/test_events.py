"""Event dispatcher: morning briefing, debrief, notifications, quiet hours (injected clock)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tests import synthetic
from thresy import store
from thresy.runner import FakeRunner, Progress

from .conftest import CHAT, local

pytestmark = pytest.mark.db


def add_event(h, kind: str, payload: dict[str, Any], *, key: str | None = None) -> int:
    return h.one(
        "INSERT INTO events (ts, kind, payload, dedupe_key) VALUES (%s, %s, %s, %s) RETURNING id",
        (h.clock.now(), kind, json.dumps(payload), key),
    )["id"]


def add_run(h, activity_id: int, start: datetime, distance: float = 10200.0) -> None:
    start_utc = start.astimezone(UTC)
    store.upsert_activity(
        h.conn,
        synthetic.activity(
            activity_id,
            start_local=start.strftime("%Y-%m-%d %H:%M:%S"),
            start_gmt=start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            distance=distance,
            moving=3130.0,
        ),
        source="test",
    )


def add_sleep(h, wake_date: str) -> None:
    h.conn.execute(
        "INSERT INTO wellness_daily (wake_date, sleep_s, sleep_score, hrv_night_avg,"
        " hrv_balanced_low, hrv_balanced_high, resting_hr, sleep_synced_at)"
        " VALUES (%s, 27120, 81, 52, 45, 60, 48, %s)",
        (wake_date, h.clock.now()),
    )


def processed(h) -> dict[int, str | None]:
    return {r["id"]: r["processed_by"] for r in h.rows("SELECT id, processed_by FROM events")}


def runs(h) -> list[str]:
    return [r["trigger"] for r in h.rows("SELECT trigger FROM agent_runs ORDER BY id")]


def briefing_runner() -> FakeRunner:
    return FakeRunner(lambda req: [Progress("text", "Good morning! Here is your briefing.")])


def test_sleep_arrived_today_starts_one_briefing_per_day(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner()) as h:
            first = add_event(
                h, "sleep_arrived", {"wake_date": "2030-01-02"}, key="sleep:2030-01-02"
            )
            await h.tick()
            assert runs(h) == ["morning_briefing"]
            prompt = h.runner.requests[0].prompt
            assert "Nobody wrote" in prompt and "morning briefing for Wed 2030-01-02" in prompt
            assert "has arrived in the database" in prompt
            assert "playbooks/morning-briefing.md yet" in prompt  # no workspace playbook yet
            assert "NO_REPLY" in prompt and "Reply in English" in prompt
            assert h.transport.texts == ["Good morning! Here is your briefing."]
            assert processed(h)[first] == "bridge:briefing"

            again = add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            h.clock.advance(3600)
            await h.tick()
            assert runs(h) == ["morning_briefing"]
            assert processed(h)[again] == "bridge:already-briefed"
            # Past the fallback time nothing new happens either.
            h.clock.set(local(2030, 1, 2, 11, 0))
            await h.tick()
            assert runs(h) == ["morning_briefing"]

    asyncio.run(scenario())


def test_playbook_is_named_when_the_workspace_has_it(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner(), start=False) as h:
            (h.workspace / "playbooks").mkdir(parents=True)
            (h.workspace / "playbooks" / "morning-briefing.md").write_text("# Briefing\n")
            await h.bridge.start(background=False)
            add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            await h.tick()
            prompt = h.runner.requests[0].prompt
            assert "Follow playbooks/morning-briefing.md." in prompt
            assert "yet:" not in prompt

    asyncio.run(scenario())


def test_sleep_for_another_day_or_too_late_is_skipped(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner()) as h:
            old = add_event(h, "sleep_arrived", {"wake_date": "2030-01-01"})
            await h.tick()
            assert runs(h) == [] and processed(h)[old] == "bridge:not-today"
            h.clock.set(local(2030, 1, 2, 15, 0))
            late = add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            await h.tick()
            assert runs(h) == [] and processed(h)[late] == "bridge:too-late"

    asyncio.run(scenario())


def test_briefing_goes_out_unsynced_at_the_fallback_time(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner()) as h:
            h.clock.set(local(2030, 1, 2, 9, 59))
            await h.tick()
            assert runs(h) == []
            h.clock.set(local(2030, 1, 2, 10, 0))
            await h.tick()
            assert runs(h) == ["morning_briefing"]
            prompt = h.runner.requests[0].prompt
            assert "had not been synced by 10:00" in prompt
            assert "do not present an older night as last night" in prompt
            late = add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            h.clock.advance(600)
            await h.tick()
            assert runs(h) == ["morning_briefing"]
            assert processed(h)[late] == "bridge:already-briefed"
            # The next day starts over.
            h.clock.set(local(2030, 1, 3, 7, 0))
            add_event(h, "sleep_arrived", {"wake_date": "2030-01-03"})
            await h.tick()
            assert runs(h) == ["morning_briefing", "morning_briefing"]

    asyncio.run(scenario())


def test_fallback_notices_sleep_that_arrived_without_an_event(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner()) as h:
            add_sleep(h, "2030-01-02")
            h.clock.set(local(2030, 1, 2, 10, 5))
            await h.tick()
            assert "has arrived in the database" in h.runner.requests[0].prompt

    asyncio.run(scenario())


def test_briefing_once_per_day_survives_a_restart(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(briefing_runner()) as h:
            add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            await h.tick()
        runner = briefing_runner()
        async with make_harness(runner) as h2:
            h2.clock.set(local(2030, 1, 2, 10, 30))
            again = add_event(h2, "sleep_arrived", {"wake_date": "2030-01-02"})
            await h2.tick()
            assert runner.requests == []
            assert processed(h2)[again] == "bridge:already-briefed"

    asyncio.run(scenario())


def test_debrief_waits_and_merges_uploads_in_a_row(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(lambda req: [Progress("text", "Nice run!")])
        async with make_harness(runner) as h:
            add_run(h, 5001, local(2030, 1, 2, 7, 0))
            add_run(h, 5002, local(2030, 1, 2, 7, 40), distance=3000)
            a = add_event(
                h, "new_activity", {"activity_id": 5001, "type": "running", "is_run": True}
            )
            await h.tick()
            assert runs(h) == []  # waiting for more uploads
            h.clock.advance(120)
            b = add_event(
                h, "new_activity", {"activity_id": 5002, "type": "running", "is_run": True}
            )
            await h.tick()
            h.clock.advance(170)
            await h.tick()
            assert runs(h) == []  # the second upload restarted the delay
            h.clock.advance(15)
            await h.tick()
            assert runs(h) == ["post_run"]
            prompt = runner.requests[0].prompt
            assert "New runs uploaded: activity 5001" in prompt and "activity 5002" in prompt
            assert "Debrief them in one message" in prompt
            assert "started 2030-01-02 07:00 local, 10.2 km" in prompt
            assert h.transport.texts == ["Nice run!"]
            assert processed(h)[a] == processed(h)[b] == "bridge:debrief"

    asyncio.run(scenario())


def test_debrief_delay_is_capped(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(bridge={"bridge_debrief_max_wait_s": 300}) as h:
            for i in range(4):
                add_run(h, 6000 + i, local(2030, 1, 2, 7, i))
                add_event(h, "new_activity", {"activity_id": 6000 + i, "is_run": True})
                await h.tick()
                h.clock.advance(100)
            assert runs(h) == ["post_run"]  # first upload + 300 s, despite new uploads

    asyncio.run(scenario())


def test_debrief_skips_other_sports_and_old_runs(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            add_run(h, 7001, local(2029, 12, 30, 7, 0))
            ride = add_event(
                h, "new_activity", {"activity_id": 7000, "type": "cycling", "is_run": False}
            )
            old = add_event(
                h, "new_activity", {"activity_id": 7001, "type": "running", "is_run": True}
            )
            await h.tick()
            h.clock.advance(3600)
            await h.tick()
            assert runs(h) == []
            assert processed(h) == {ride: "bridge:not-a-run", old: "bridge:too-old"}

    asyncio.run(scenario())


def test_quiet_hours_hold_proactive_messages(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(bridge={"bridge_quiet_hours": "22:00-06:30"}) as h:
            h.clock.set(local(2030, 1, 2, 5, 0))
            event = add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            add_event(h, "garmin_auth_required", {"reason": "token expired"})
            await h.tick()
            assert runs(h) == [] and h.transport.sent == []
            assert processed(h)[event] is None  # still waiting, also across a restart
            h.clock.set(local(2030, 1, 2, 6, 31))
            await h.tick()
            assert runs(h) == ["morning_briefing"]
            assert any("Garmin sync needs attention" in t for t in h.transport.texts)
            # Answers to the athlete are never held back.
            h.clock.set(local(2030, 1, 2, 23, 0))
            await h.say("still awake")
            await h.settle()
            assert runs(h) == ["morning_briefing", "user_message"]

    asyncio.run(scenario())


def test_briefing_and_debrief_due_together_become_one_run(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(lambda req: [Progress("text", "Morning! And about that run...")])
        async with make_harness(runner, bridge={"bridge_quiet_hours": "22:00-07:00"}) as h:
            h.clock.set(local(2030, 1, 2, 6, 30))
            add_run(h, 8001, local(2030, 1, 2, 5, 45))
            add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            add_event(h, "new_activity", {"activity_id": 8001, "type": "running", "is_run": True})
            await h.tick()
            h.clock.set(local(2030, 1, 2, 7, 0))
            await h.tick()
            assert runs(h) == ["morning_briefing+post_run"]
            prompt = runner.requests[0].prompt
            assert "Several things are due" in prompt
            assert "morning briefing" in prompt and "post-run debrief" in prompt
            assert len(h.transport.sent) == 1
            assert all(
                p == "bridge:briefing" or p == "bridge:debrief" for p in processed(h).values()
            )

    asyncio.run(scenario())


def test_problem_notifications_are_plain_and_rate_limited(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(bridge={"bridge_briefing": False}) as h:
            first = add_event(
                h, "garmin_auth_required", {"reason": "401 from Garmin"}, key="garmin_auth:a"
            )
            await h.tick()
            assert h.runner.requests == []  # no agent run
            assert len(h.transport.texts) == 1
            assert "Garmin sync needs attention" in h.transport.texts[0]
            assert "Last error: 401 from Garmin" in h.transport.texts[0]
            h.clock.advance(hours=6)
            second = add_event(h, "garmin_auth_required", {}, key="garmin_auth:b")
            failed = add_event(h, "sync_failed", {"error": "x"}, key="sync_failed:1")
            failed2 = add_event(h, "sync_failed", {"error": "x"}, key="sync_failed:2")
            await h.tick()
            assert len(h.transport.texts) == 1
            states = processed(h)
            assert states[first] == "bridge:notified"
            assert states[second] == "bridge:rate-limited"
            assert states[failed] == states[failed2] == "bridge:covered-by-auth"
            h.clock.advance(hours=7)
            add_event(h, "garmin_auth_required", {}, key="garmin_auth:c")
            await h.tick()
            assert len(h.transport.texts) == 2

    asyncio.run(scenario())


def test_sync_failures_notify_only_when_persistent(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(
            settings={"language": "de"}, bridge={"bridge_briefing": False}
        ) as h:
            one = add_event(h, "sync_failed", {"error": "HTTP 503"})
            await h.tick()
            assert h.transport.texts == []
            assert processed(h)[one] == "bridge:below-threshold"
            h.clock.advance(hours=1)
            add_event(h, "sync_failed", {"error": "HTTP 503"})
            await h.tick()
            assert len(h.transport.texts) == 1
            text = h.transport.texts[0]
            assert "seit 08:00 2-mal fehlgeschlagen" in text and "HTTP 503" in text
            h.clock.advance(hours=1)
            add_event(h, "sync_failed", {"error": "HTTP 503"})
            await h.tick()
            assert len(h.transport.texts) == 1  # rate-limited

    asyncio.run(scenario())


def test_unknown_events_are_marked_and_left_alone(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            event = add_event(h, "something_new", {})
            await h.tick()
            assert processed(h)[event] == "bridge:ignored"

    asyncio.run(scenario())


def test_numbers_only_briefing_when_the_usage_limit_is_reached(make_harness) -> None:
    async def scenario() -> None:
        runner = FakeRunner(fail="rate_limit", fail_detail="limit resets 3pm")
        async with make_harness(runner) as h:
            add_sleep(h, "2030-01-02")
            add_run(h, 9001, local(2030, 1, 1, 18, 0))
            add_event(h, "sleep_arrived", {"wake_date": "2030-01-02"})
            await h.tick()
            assert len(h.transport.texts) == 2
            assert h.transport.texts[0].startswith("Morning briefing: I can't answer right now")
            numbers = h.transport.texts[1]
            assert numbers.splitlines() == [
                "**Morning numbers (no coach commentary today)** · 2030-01-02",
                "Sleep: 7:32 h · score 81",
                "HRV: 52 ms (balanced range 45–60: within)",
                "Resting HR: 48 bpm",
                "Yesterday: 10.2 km in 52:10 · 5:06/km · avg HR 140",
            ]

    asyncio.run(scenario())


def test_numbers_only_briefing_says_when_sleep_is_missing(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(FakeRunner(fail="auth"), settings={"language": "de"}) as h:
            h.clock.set(local(2030, 1, 2, 10, 0))
            await h.tick()
            assert h.transport.texts[0].startswith("Morgen-Briefing: Ich kann nicht antworten")
            assert h.transport.texts[1].splitlines()[1:] == [
                "Der Schlaf der letzten Nacht ist noch nicht synchronisiert.",
                "Gestern: kein Lauf.",
            ]
            assert h.one("SELECT chat_id FROM outbox LIMIT 1")["chat_id"] == CHAT

    asyncio.run(scenario())
