"""Chat loop: messages in and out, sessions, queueing, failures."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from thresy.bridge import prompts
from thresy.bridge.chat import Job
from thresy.runner import Fail, FakeRunner, Progress, Started

from .conftest import CHAT, TOKEN, GatedRunner, OtherRunner

pytestmark = pytest.mark.db


def answering(prefix: str = "answer") -> FakeRunner:
    """A runner that answers "<prefix> <n>" and reports one harness session per fresh run."""
    counter = {"n": 0}

    def script(req):
        counter["n"] += 1
        items = [] if req.session_id else [Started(f"native-{counter['n']}")]
        return [*items, Progress("text", f"{prefix} {counter['n']}")]

    return FakeRunner(script)


def test_message_in_answer_out_both_logged(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("How was my run?")
            await h.settle()
            assert h.transport.texts == ["answer 1"]
            assert h.transport.sent[0].item.chat_id == CHAT
            assert h.transport.busy_log == [(CHAT, True), (CHAT, False)]
            msgs = h.rows(
                "SELECT direction, kind, text, run_id, transport FROM messages ORDER BY id"
            )
            run = h.one("SELECT * FROM agent_runs")
            assert [(m["direction"], m["kind"], m["text"]) for m in msgs] == [
                ("in", "text", "How was my run?"),
                ("out", "text", "answer 1"),
            ]
            assert {m["run_id"] for m in msgs} == {run["id"]}
            assert {m["transport"] for m in msgs} == {"fake"}
            assert run["trigger"] == "user_message" and run["status"] == "ok"
            assert run["runner"] == "fake" and run["model"] == "fake-model"
            assert run["finished_at"] is not None
            outbox = h.one("SELECT * FROM outbox")
            assert outbox["sent_at"] is not None and outbox["run_id"] == run["id"]
            prompt = h.runner.requests[0].prompt
            assert "How was my run?" in prompt and "Reply in English" in prompt
            assert h.runner.requests[0].cwd == h.workspace
            assert h.runner.requests[0].system_append is None

    asyncio.run(scenario())


def test_session_created_resumed_and_replaced_on_a_new_day(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("first")
            await h.settle()
            session = h.one("SELECT * FROM agent_sessions")
            assert session["native_session_id"] == "native-1" and session["runner"] == "fake"
            assert session["day"].isoformat() == "2030-01-02"
            assert h.runner.requests[0].session_id is None

            await h.say("second")
            await h.settle()
            assert h.runner.requests[1].session_id == "native-1"
            assert "Earlier messages" not in h.runner.requests[1].prompt
            assert h.one("SELECT count(*) AS n FROM agent_sessions")["n"] == 1

            h.clock.advance(hours=24)
            await h.say("next day")
            await h.settle()
            third = h.runner.requests[2]
            assert third.session_id is None
            # The fresh session gets yesterday's conversation from the log, oldest first.
            assert "Earlier messages from the chat log" in third.prompt
            assert third.prompt.index("Athlete: first") < third.prompt.index("Coach: answer 2")
            assert "next day" not in third.prompt.split("(end of earlier messages)")[0]
            sessions = h.rows(
                "SELECT day, status, native_session_id FROM agent_sessions ORDER BY started_at"
            )
            assert [(s["day"].isoformat(), s["status"]) for s in sessions] == [
                ("2030-01-02", "closed"),
                ("2030-01-03", "active"),
            ]
            assert sessions[1]["native_session_id"] == "native-3"

    asyncio.run(scenario())


def test_new_command_starts_a_fresh_session_without_old_context(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("remember this")
            await h.settle()
            await h.say("/new")
            await h.settle()
            assert h.transport.texts[-1].startswith("Fresh start")
            await h.say("hello again")
            await h.settle()
            req = h.runner.requests[-1]
            assert req.session_id is None
            assert "remember this" not in req.prompt
            statuses = [
                r["status"] for r in h.rows("SELECT status FROM agent_sessions ORDER BY started_at")
            ]
            assert statuses == ["reset", "active"]
            # The command is logged, but not as a message to the agent.
            assert h.one("SELECT kind FROM messages WHERE text = '/new'")["kind"] == "command"

    asyncio.run(scenario())


def test_runner_change_rebuilds_the_session_from_the_log(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("my knee hurts a bit")
            await h.settle()
        other = OtherRunner(reply="noted")
        async with make_harness(other) as h2:
            await h2.say("what did I tell you?")
            await h2.settle()
            req = other.requests[0]
            assert req.session_id is None
            assert "Athlete: my knee hurts a bit" in req.prompt
            sessions = h2.rows("SELECT runner, status FROM agent_sessions ORDER BY started_at")
            assert [(s["runner"], s["status"]) for s in sessions] == [
                ("fake", "closed"),
                ("other-fake", "active"),
            ]

    asyncio.run(scenario())


def test_messages_during_a_run_are_queued_and_handed_over_together(make_harness) -> None:
    async def scenario() -> None:
        runner = GatedRunner(lambda req: [Progress("text", f"reply to {len(runner.requests)}")])
        async with make_harness(runner) as h:
            await h.say("alpha")
            await asyncio.wait_for(runner.started.wait(), 5)
            h.clock.advance(60)
            await h.say("bravo")
            await h.say("charlie")
            await asyncio.sleep(0.05)
            assert len(runner.requests) == 1  # no concurrent run
            runner.gate.set()
            await h.settle()
            assert len(runner.requests) == 2
            second = runner.requests[1].prompt
            assert "sent 2 messages" in second
            assert "[08:01] bravo\n\n[08:01] charlie" in second
            assert "alpha" not in second
            assert h.transport.texts == ["reply to 1", "reply to 2"]
            runs = h.rows("SELECT id FROM agent_runs ORDER BY id")
            linked = h.rows("SELECT text, run_id FROM messages WHERE direction = 'in' ORDER BY id")
            assert [(m["text"], m["run_id"]) for m in linked] == [
                ("alpha", runs[0]["id"]),
                ("bravo", runs[1]["id"]),
                ("charlie", runs[1]["id"]),
            ]

    asyncio.run(scenario())


BURST = ["msg-alpha", "msg-bravo", "/status", "msg-charlie", "msg-delta"]


def test_messages_waiting_at_start_up_go_to_one_run(make_harness) -> None:
    """Messages sent while the bridge was down arrive in one burst when it starts: the
    transport hands them over one after another, each logged before the next. They go to one
    run, none of them a second time as earlier context, and the command among them is answered
    by the bridge itself."""

    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("yesterday's question")
            await h.settle()
            h.clock.advance(hours=24)  # a fresh session, so the prompt carries context
            for text in BURST:
                await h.say(text)
            await h.settle()

            assert len(h.runner.requests) == 2  # yesterday's run and one for the burst
            prompt = h.runner.requests[1].prompt
            context, _, waiting = prompt.partition("(end of earlier messages)")
            assert "Athlete: yesterday's question" in context and "Coach: answer 1" in context
            assert "sent 4 messages" in waiting
            for text in BURST:
                if text.startswith("/"):
                    assert text not in prompt
                else:
                    assert text not in context and waiting.count(text) == 1

            runs = h.rows("SELECT id, trigger FROM agent_runs ORDER BY id")
            assert len(runs) == 2 and runs[1]["trigger"] == "user_message"
            linked = h.rows(
                "SELECT text, kind, run_id FROM messages WHERE direction = 'in' AND ts >= %s"
                " ORDER BY id",
                (h.clock.now(),),
            )
            assert [(m["text"], m["kind"], m["run_id"]) for m in linked] == [
                (text, "command", None) if text == "/status" else (text, "text", runs[1]["id"])
                for text in BURST
            ]
            # The command was answered without a run, the messages with one answer.
            texts = h.transport.texts
            assert len(texts) == 3 and texts[0] == "answer 1"
            assert "answer 2" in texts[1:]
            assert any(t.startswith("**Status**") for t in texts[1:])

    asyncio.run(scenario())


def test_messages_arriving_while_a_run_starts_are_not_shown_twice(make_harness) -> None:
    """A message logged after a run took its messages, while that run is still being prepared,
    waits for the next run: it is not shown to the first one as earlier context."""

    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("yesterday's question")
            await h.settle()
            h.clock.advance(hours=24)
            service = h.bridge.service
            original = service.context_for

            async def late_message(chat_id, session, spec):
                # Logged (and handed to the worker) after this run's prompt took its messages.
                if not getattr(late_message, "done", False):
                    late_message.done = True
                    await h.say("msg-late")
                return await original(chat_id, session, spec)

            service.context_for = late_message
            await h.say("msg-first")
            await h.settle()

            assert len(h.runner.requests) == 3
            first, second = h.runner.requests[1].prompt, h.runner.requests[2].prompt
            assert "msg-first" in first and "msg-late" not in first
            assert "msg-late" in second and "msg-first" not in second
            linked = h.rows("SELECT text, run_id FROM messages WHERE text LIKE 'msg-%%'")
            assert len({m["run_id"] for m in linked}) == 2

    asyncio.run(scenario())


def test_a_proactive_run_does_not_show_a_waiting_message_as_context(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            await h.say("yesterday's question")
            await h.settle()
            h.clock.advance(hours=24)

            async def build() -> prompts.ProactiveSection:
                await h.say("msg-during-the-briefing")  # arrives while the run is prepared
                return prompts.ProactiveSection("morning_briefing", "Trigger: test.", "x.md", "")

            h.bridge.enqueue_job(CHAT, Job(build=build, what="what_morning_briefing"))
            await h.settle()

            assert len(h.runner.requests) == 3
            briefing, chat = h.runner.requests[1].prompt, h.runner.requests[2].prompt
            assert "Nobody wrote" in briefing and "Athlete: yesterday's question" in briefing
            assert "msg-during-the-briefing" not in briefing
            assert "msg-during-the-briefing" in chat

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("rate_limit", "usage limit of the coach's AI subscription"),
        ("auth", "login of the coach's AI agent has expired"),
        ("timeout", "took too long"),
        ("crash", "something went wrong"),
        ("other", "something went wrong"),
    ],
)
def test_each_error_kind_gets_its_message(make_harness, kind, expected) -> None:
    async def scenario() -> None:
        runner = FakeRunner(fail=kind, fail_detail="resets at 5pm")
        async with make_harness(runner) as h:
            await h.say("hello")
            await h.settle()
            assert len(h.transport.texts) == 1
            text = h.transport.texts[0]
            assert expected in text
            if kind in ("rate_limit", "auth"):
                assert "resets at 5pm" in text
            run = h.one("SELECT status, error_kind, error_detail FROM agent_runs")
            assert run == {"status": "failed", "error_kind": kind, "error_detail": "resets at 5pm"}
            # A fresh session failing is not retried.
            assert len(runner.requests) == 1

    asyncio.run(scenario())


def test_error_messages_follow_the_language(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(FakeRunner(fail="rate_limit"), settings={"language": "de"}) as h:
            await h.say("Hallo")
            await h.settle()
            assert "Nutzungslimit" in h.transport.texts[0]
            assert "Reply in German" in h.runner.requests[0].prompt

    asyncio.run(scenario())


def test_failed_resume_is_retried_once_without_the_session(make_harness) -> None:
    async def scenario() -> None:
        def script(req):
            if req.session_id:
                return [Fail("other", "No conversation found with session ID")]
            return [Started(f"native-{len(runner.requests)}"), Progress("text", "fresh answer")]

        runner = FakeRunner(script)
        async with make_harness(runner) as h:
            await h.say("first")
            await h.settle()
            await h.say("second")
            await h.settle()
            assert [r.session_id for r in runner.requests] == [None, "native-1", None]
            assert "Athlete: first" in runner.requests[2].prompt  # context from the log
            assert h.transport.texts == ["fresh answer", "fresh answer"]
            runs = h.rows("SELECT status, error_kind FROM agent_runs ORDER BY id")
            assert [(r["status"], r["error_kind"]) for r in runs] == [
                ("ok", None),
                ("failed", "other"),
                ("ok", None),
            ]
            sessions = h.rows(
                "SELECT status, native_session_id FROM agent_sessions ORDER BY started_at"
            )
            assert [s["status"] for s in sessions] == ["failed", "active"]
            assert sessions[1]["native_session_id"] == "native-3"

    asyncio.run(scenario())


def test_failed_resume_is_retried_only_once(make_harness) -> None:
    async def scenario() -> None:
        def script(req):
            if len(runner.requests) == 1:
                return [Started("native-1"), Progress("text", "ok")]
            return [Fail("crash", "exit 1")]

        runner = FakeRunner(script)
        async with make_harness(runner) as h:
            await h.say("first")
            await h.settle()
            await h.say("second")
            await h.settle()
            assert len(runner.requests) == 3
            assert "something went wrong" in h.transport.texts[-1]
            assert len(h.transport.texts) == 2

    asyncio.run(scenario())


def test_no_reply_and_empty_answers(make_harness) -> None:
    async def scenario() -> None:
        replies = iter(["NO_REPLY", ""])
        runner = FakeRunner(lambda req: [Progress("text", next(replies))])
        async with make_harness(runner) as h:
            await h.say("thanks!")
            await h.settle()
            assert h.transport.texts == []  # the agent chose not to answer
            await h.say("and?")
            await h.settle()
            assert h.transport.texts == ["Sorry, I ended up without an answer. Please ask again."]

    asyncio.run(scenario())


def test_agent_environment_is_exactly_what_it_needs(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(FakeRunner(reply="ok")) as h:
            await h.say("hi")
            await h.settle()
            env = dict(h.runner.requests[0].env)
            run_id = h.one("SELECT id FROM agent_runs")["id"]
            assert env == {
                "COACH_DATABASE_URL": h.settings.agent_database_url,
                "COACH_DATABASE_URL_RO": h.settings.database_url_ro,
                "COACH_DATA_DIR": str((h.workspace.parent / "data").resolve()),
                "COACH_WORKSPACE_DIR": str(h.workspace),
                "COACH_TZ": "America/Chicago",
                "TZ": "America/Chicago",
                "COACH_LANGUAGE": "en",
                "COACH_NAME": "Thresy",
                "COACH_RUN_ID": str(run_id),
            }
            values = "\n".join(env.values())
            assert TOKEN not in values
            assert h.settings.database_url not in env.values()
            assert "coach_app" not in values

    asyncio.run(scenario())


def test_agent_gets_the_home_location_from_the_bridge(make_harness) -> None:
    async def scenario() -> None:
        bridge = {"home_location": " Springfield, Illinois "}
        async with make_harness(FakeRunner(reply="ok"), bridge=bridge) as h:
            await h.say("weather?")
            await h.settle()
            env = dict(h.runner.requests[0].env)
            assert env["COACH_HOME_LOCATION"] == "Springfield, Illinois"

    asyncio.run(scenario())


def test_choice_answer_comes_back_as_a_message_once(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(answering()) as h:
            outbox_id = h.one(
                "INSERT INTO outbox (chat_id, kind, text, options) VALUES (%s, 'choice',"
                " 'Tomorrow?', '[\"Easy\", \"Rest\"]') RETURNING id",
                (CHAT,),
            )["id"]
            await h.settle()
            assert h.transport.sent[0].item.options == ("Easy", "Rest")
            assert h.transport.sent[0].item.ref == str(outbox_id)
            await h.say("", kind="choice_answer", reply_to=str(outbox_id), choice_index=1)
            await h.say("Rest", kind="choice_answer", reply_to=str(outbox_id), choice_index=1)
            await h.settle()
            assert len(h.runner.requests) == 1
            prompt = h.runner.requests[0].prompt
            assert 'pressed the button "Rest"' in prompt.replace("Pressed", "pressed")
            assert 'answering your question: "Tomorrow?"' in prompt
            row = h.one("SELECT kind, text, attachments FROM messages WHERE direction = 'in'")
            assert row["kind"] == "choice_answer" and row["text"] == "Rest"
            assert row["attachments"] == {"reply_to": str(outbox_id), "choice_index": 1}

    asyncio.run(scenario())


def test_attachments_are_described_to_the_agent(make_harness) -> None:
    from thresy.transport import Attachment

    async def scenario() -> None:
        async with make_harness(FakeRunner(reply="nice photo")) as h:
            saved = h.workspace / "inbox" / "2030-01-02" / "7-photo.jpg"
            saved.parent.mkdir(parents=True)
            saved.write_bytes(b"jpeg")
            voice = Attachment(kind="voice", path=None, duration_s=12, error="download failed")
            await h.say(
                "look",
                kind="photo",
                attachments=(Attachment(kind="photo", path=saved, mime_type="image/jpeg"), voice),
            )
            await h.settle()
            prompt = h.runner.requests[0].prompt
            assert "[photo at inbox/2030-01-02/7-photo.jpg]" in prompt
            assert "voice (could not be saved: download failed)" in prompt
            row = h.one("SELECT attachments FROM messages WHERE direction = 'in'")
            assert row["attachments"][0]["path"] == "inbox/2030-01-02/7-photo.jpg"

    asyncio.run(scenario())


def test_messages_from_other_chats_are_dropped(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            await h.say("let me in", chat="999")
            await h.settle()
            assert h.runner.requests == []
            assert h.rows("SELECT * FROM messages") == []
            assert len(h.transport.dropped) == 1

    asyncio.run(scenario())


def test_run_timeout_from_settings(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(FakeRunner(reply="x"), bridge={"bridge_run_timeout_s": 42}) as h:
            await h.say("hi")
            await h.settle()
            assert h.runner.requests[0].timeout_s == 42
            assert h.runner.requests[0].model_tier == "smart"
            assert h.runner.requests[0].effort is None  # unset: the harness's default

    asyncio.run(scenario())


def test_context_is_bounded(make_harness) -> None:
    async def scenario() -> None:
        async with make_harness(
            answering(), bridge={"bridge_context_messages": 3, "bridge_context_chars": 10_000}
        ) as h:
            for i in range(5):
                await h.say(f"message {i}")
                await h.settle()
            h.clock.advance(hours=24)
            await h.say("new day")
            await h.settle()
            context = h.runner.requests[-1].prompt.split("(end of earlier messages)")[0]
            assert context.count("] Athlete:") + context.count("] Coach:") == 3
            assert "message 4" in context and "message 0" not in context
            h.clock.advance(timedelta(hours=60).total_seconds())
            await h.say("much later")
            await h.settle()
            assert "Earlier messages" not in h.runner.requests[-1].prompt

    asyncio.run(scenario())
