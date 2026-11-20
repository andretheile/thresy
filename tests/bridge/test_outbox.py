"""Outbox delivery: in order, once, across restarts; failures recorded; files checked."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from tests.tools.test_send import PNG, coach
from thresy.runner import FakeRunner, Progress
from thresy.transport import DeliveryError, FakeTransport

from .conftest import CHAT

pytestmark = pytest.mark.db


def test_coach_send_rows_are_delivered_in_order_before_the_answer(make_harness) -> None:
    results = []

    def script(req):
        env = dict(req.env)
        out = Path(env["COACH_WORKSPACE_DIR"]) / "out"
        out.mkdir(exist_ok=True)
        (out / "plot.png").write_bytes(PNG)
        # The agent's own tool, with exactly the environment the bridge gave the agent.
        results.append(coach(["send", "text", "Here is the plot"], env))
        results.append(coach(["send", "photo", str(out / "plot.png"), "-c", "HR zones"], env))
        results.append(coach(["send", "choice", "Tomorrow?", "-o", "Easy", "-o", "Rest"], env))
        return [Progress("text", "Final words")]

    async def scenario() -> None:
        async with make_harness(FakeRunner(script)) as h:
            await h.say("show me my zones")
            await h.settle()
            assert all(r.exit_code == 0 for r in results), [r.output for r in results]
            sent = [(r.item.kind, r.item.text) for r in h.transport.sent]
            assert sent == [
                ("text", "Here is the plot"),
                ("photo", "HR zones"),
                ("choice", "Tomorrow?"),
                ("text", "Final words"),
            ]
            assert h.transport.sent[1].item.file_path == (h.workspace / "out" / "plot.png")
            assert h.transport.sent[1].item.data == PNG  # read by the bridge, once
            assert all(r.item.chat_id == CHAT for r in h.transport.sent)
            run_id = h.one("SELECT id FROM agent_runs")["id"]
            rows = h.rows(
                "SELECT run_id, sent_at, transport_msg_id, chat_id FROM outbox ORDER BY id"
            )
            assert all(r["run_id"] == run_id and r["sent_at"] is not None for r in rows)
            assert [r["transport_msg_id"] for r in rows] == ["m1", "m2", "m3", "m4"]
            assert all(r["chat_id"] == CHAT for r in rows)  # resolved through the run
            out = h.rows(
                "SELECT kind, run_id, attachments FROM messages WHERE direction = 'out' ORDER BY id"
            )
            assert [m["kind"] for m in out] == ["text", "photo", "choice", "text"]
            assert out[2]["attachments"] == {"options": ["Easy", "Rest"]}
            # Nothing is sent twice.
            await h.settle()
            assert len(h.transport.sent) == 4

    asyncio.run(scenario())


def test_delivered_rows_are_not_resent_after_a_restart(make_harness) -> None:
    async def scenario() -> None:
        first = FakeTransport(chat_ids=(CHAT,))
        async with make_harness(transport=first, start=False) as h:
            for text in ("one", "two", "three"):
                h.conn.execute(
                    "INSERT INTO outbox (chat_id, kind, text) VALUES (%s, 'text', %s)", (CHAT, text)
                )
            first.fail_next(None, DeliveryError("connection reset", permanent=False))
            await h.bridge.start(background=False)
            await h.bridge.outbox.deliver_pending()
            assert first.texts == ["one"]
            # A transient failure keeps the order: "three" waits behind "two".
            row = h.one("SELECT sent_at, error FROM outbox WHERE text = 'two'")
            assert row == {"sent_at": None, "error": None}
        second = FakeTransport(chat_ids=(CHAT,))
        async with make_harness(transport=second) as h2:
            await h2.settle()
            assert second.texts == ["two", "three"]
            assert h2.one("SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL")["n"] == 0
            assert h2.one("SELECT count(*) AS n FROM messages WHERE direction = 'out'")["n"] == 3

    asyncio.run(scenario())


def test_transient_failures_back_off_then_give_up(make_harness) -> None:
    async def scenario() -> None:
        transport = FakeTransport(chat_ids=(CHAT,))
        async with make_harness(transport=transport, bridge={"bridge_send_attempts": 3}) as h:
            h.conn.execute(
                "INSERT INTO outbox (chat_id, kind, text) VALUES (%s, 'text', 'a')", (CHAT,)
            )
            h.conn.execute(
                "INSERT INTO outbox (chat_id, kind, text) VALUES (%s, 'text', 'b')", (CHAT,)
            )
            flaky = DeliveryError("timed out", permanent=False)
            transport.fail_next(flaky, flaky, flaky)
            assert await h.bridge.outbox.deliver_pending() == 0
            # Backing off: nothing is tried before the retry time.
            assert await h.bridge.outbox.deliver_pending() == 0
            h.clock.advance(6)
            assert await h.bridge.outbox.deliver_pending() == 0
            h.clock.advance(11)
            assert await h.bridge.outbox.deliver_pending() == 1
            assert transport.texts == ["b"]
            error = h.one("SELECT error FROM outbox WHERE text = 'a'")["error"]
            assert error == "gave up after 3 attempts: timed out"

    asyncio.run(scenario())


def test_rows_the_agent_wrote_with_sql_are_checked_again(make_harness, tmp_path) -> None:
    async def scenario() -> None:
        async with make_harness() as h:
            secret = tmp_path / "secret.txt"
            secret.write_text("do not send")
            insert = (
                "INSERT INTO outbox (chat_id, kind, text, file_path, options)"
                " VALUES (%s, %s, %s, %s, %s)"
            )
            h.conn.execute(insert, (None, "document", None, str(secret), None))
            h.conn.execute(insert, (None, "photo", None, "../secret.txt", None))
            h.conn.execute(insert, ("999", "text", "spam", None, None))
            h.conn.execute(insert, (None, "video", "x", None, None))
            h.conn.execute(insert, (None, "choice", "Q?", None, '["a", 3]'))
            h.conn.execute(insert, (None, "text", "  ", None, None))
            h.conn.execute(insert, (None, "text", "still delivered", None, None))
            await h.settle()
            assert h.transport.texts == ["still delivered"]
            errors = [r["error"] for r in h.rows("SELECT error FROM outbox ORDER BY id")]
            assert "must be under" in errors[0]
            assert "must be under" in errors[1] or "not found" in errors[1]
            assert "not allow-listed" in errors[2]
            assert "unknown kind" in errors[3]
            assert "invalid options" in errors[4]
            assert "empty text" in errors[5]
            assert errors[6] is None

    asyncio.run(scenario())


def test_files_swapped_for_links_after_coach_send_are_refused(make_harness, tmp_path) -> None:
    """The agent queues files that pass `coach send`'s check, then swaps them for links to
    something only the bridge may read (in the container, e.g. /proc/self/environ)."""
    secret = b"COACH_TELEGRAM_TOKEN=bridge-only"
    bridge_only = tmp_path / "bridge-only"
    bridge_only.mkdir()
    (bridge_only / "environ").write_bytes(secret)
    (bridge_only / "data.csv").write_bytes(secret)
    results = []

    def script(req):
        env = dict(req.env)
        out = Path(env["COACH_WORKSPACE_DIR"]) / "out"
        (out / "sub").mkdir(parents=True, exist_ok=True)
        (out / "plot.png").write_bytes(PNG)
        (out / "sub" / "data.csv").write_text("a,b\n1,2\n")
        (out / "fine.png").write_bytes(PNG)
        results.append(coach(["send", "photo", str(out / "plot.png")], env))
        results.append(coach(["send", "document", str(out / "sub" / "data.csv")], env))
        results.append(coach(["send", "photo", str(out / "fine.png"), "-c", "still fine"], env))
        # After the check, before the bridge reads: a link as the file, a link as a parent.
        (out / "plot.png").unlink()
        (out / "plot.png").symlink_to(bridge_only / "environ")
        shutil.rmtree(out / "sub")
        (out / "sub").symlink_to(bridge_only)
        return [Progress("text", "Here you go")]

    async def scenario() -> None:
        async with make_harness(FakeRunner(script)) as h:
            await h.say("send me the files")
            await h.settle()
            assert all(r.exit_code == 0 for r in results), [r.output for r in results]
            # A path that escapes out/, inserted with plain SQL.
            h.conn.execute(
                "INSERT INTO outbox (chat_id, kind, file_path) VALUES (%s, 'document', %s)",
                (CHAT, str(h.workspace / "out" / ".." / ".." / "bridge-only" / "environ")),
            )
            await h.settle()
            sent = [(r.item.kind, r.item.text, r.item.data) for r in h.transport.sent]
            assert sent == [("photo", "still fine", PNG), ("text", "Here you go", None)]
            errors = [
                r["error"]
                for r in h.rows("SELECT error FROM outbox WHERE sent_at IS NULL ORDER BY id")
            ]
            assert errors[0] == (
                "refused: out/plot.png is a symbolic link; files are never read through links"
            )
            assert errors[1] == (
                "refused: out/sub is a symbolic link; files are never read through links"
            )
            assert errors[2].startswith("document files must be under ")
            assert len(errors) == 3

    asyncio.run(scenario())


def test_bridge_messages_go_to_the_default_chat(make_harness) -> None:
    async def scenario() -> None:
        transport = FakeTransport(chat_ids=(CHAT, "200"))
        async with make_harness(transport=transport) as h:
            h.conn.execute("INSERT INTO outbox (kind, text) VALUES ('text', 'to the default chat')")
            await h.settle()
            assert transport.sent[0].item.chat_id == CHAT
            assert h.one("SELECT chat_id FROM outbox")["chat_id"] == CHAT

    asyncio.run(scenario())
