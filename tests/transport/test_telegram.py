"""The Telegram transport against a recording fake bot (no network, no real bot)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from thresy.transport import DeliveryError, InboundMessage, OutboundItem
from thresy.transport.telegram import TelegramTransport, classify_error
from thresy.transport.telegram import transport as tg
from thresy.transport.telegram.markdown import utf16_len

# Creating RetryAfter in a test triggers PTB's own warning about its int-valued attribute.
pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")

CHAT = "100"
DATE = 1893564000  # 2030-01-02 06:00 UTC
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class Sent:
    message_id: int


class FakeFile:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.data)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.next_id = 1

    def fail(self, method: str, *errors: Exception) -> None:
        self.failures.setdefault(method, []).extend(errors)

    async def _record(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, kwargs))
        queue = self.failures.get(method)
        if queue:
            raise queue.pop(0)
        self.next_id += 1
        return Sent(self.next_id)

    def __getattr__(self, method: str):
        if method.startswith("send_") or method in (
            "answer_callback_query",
            "edit_message_reply_markup",
        ):

            async def call(*args: Any, **kwargs: Any) -> Any:
                return await self._record(method, kwargs)

            return call
        raise AttributeError(method)

    async def get_file(self, file_id: str) -> FakeFile:
        self.calls.append(("get_file", {"file_id": file_id}))
        return FakeFile(f"content of {file_id}".encode())

    def named(self, method: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == method]


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def transport(bot: FakeBot, tmp_path: Path) -> TelegramTransport:
    return TelegramTransport("123:abc", [CHAT, "-200"], tmp_path / "inbox", bot=bot)


def run(coro):
    return asyncio.run(coro)


def entity_text(text: str, entity: Any) -> str:
    raw = text.encode("utf-16-le")
    return raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


# Sending ---------------------------------------------------------------------------------------


def test_text_is_sent_with_entities_and_split(transport, bot) -> None:
    text = "**Summary**\n\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(8))
    ids = run(transport.send(OutboundItem(chat_id=CHAT, kind="text", text=text)))
    sent = bot.named("send_message")
    assert len(sent) > 1 and ids == ",".join(str(i) for i in range(2, 2 + len(sent)))
    first = sent[0]
    assert first["chat_id"] == 100 and first["text"].startswith("Summary\n\nParagraph 0.")
    assert first["link_preview_options"].is_disabled is True
    (bold,) = first["entities"]
    assert bold.type == "bold" and entity_text(first["text"], bold) == "Summary"
    assert all(utf16_len(kw["text"]) <= 4096 for kw in sent)


def test_rejected_formatting_falls_back_to_plain_text(transport, bot) -> None:
    bot.fail("send_message", BadRequest("Can't parse entities: unsupported start tag"))
    ids = run(transport.send(OutboundItem(chat_id=CHAT, kind="text", text="**bold** text")))
    first, second = bot.named("send_message")
    assert first["entities"] is not None
    assert second["text"] == "bold text" and "entities" not in second
    assert ids == "2"


def test_plain_text_rejected_too_is_a_permanent_error(transport, bot) -> None:
    bot.fail("send_message", BadRequest("chat not found"))
    with pytest.raises(DeliveryError) as info:
        run(transport.send(OutboundItem(chat_id=CHAT, kind="text", text="plain")))
    assert info.value.permanent


def test_choice_has_buttons_on_the_last_chunk(transport, bot) -> None:
    item = OutboundItem(
        chat_id=CHAT, kind="choice", text="Tomorrow?", options=("Easy", "Rest", "Long"), ref="42"
    )
    run(transport.send(item))
    (sent,) = bot.named("send_message")
    rows = sent["reply_markup"].inline_keyboard
    assert [[b.text for b in row] for row in rows] == [["Easy", "Rest"], ["Long"]]
    assert [b.callback_data for row in rows for b in row] == ["c|42|0", "c|42|1", "c|42|2"]
    long = OutboundItem(chat_id=CHAT, kind="choice", text="Q?", options=("A much longer label",))
    run(transport.send(long))
    assert len(bot.named("send_message")[-1]["reply_markup"].inline_keyboard[0]) == 1


def test_photo_with_caption_and_fallbacks(transport, bot, tmp_path) -> None:
    plot = tmp_path / "plot.png"  # never opened: the transport sends the bytes it is given
    run(
        transport.send(
            OutboundItem(chat_id=CHAT, kind="photo", text="**HR** zones", file_path=plot, data=PNG)
        )
    )
    (photo,) = bot.named("send_photo")
    assert photo["photo"] == PNG and photo["filename"] == "plot.png"
    assert photo["caption"] == "HR zones" and photo["caption_entities"][0].type == "bold"

    long_caption = "word " * 400
    item = OutboundItem(chat_id=CHAT, kind="photo", text=long_caption, file_path=plot, data=PNG)
    run(transport.send(item))
    assert "caption" not in bot.named("send_photo")[-1]
    assert bot.named("send_message")[-1]["text"] == long_caption.strip()

    bot.fail("send_photo", BadRequest("PHOTO_INVALID_DIMENSIONS"))
    ids = run(transport.send(OutboundItem(chat_id=CHAT, kind="photo", file_path=plot, data=PNG)))
    assert bot.named("send_document")[-1]["document"] == PNG
    assert ids == str(bot.next_id)


def test_file_problems_are_permanent(transport, bot, tmp_path) -> None:
    # A path alone is not enough: the transport never reads a file the agent could swap.
    secret = tmp_path / "secret.csv"
    secret.write_text("do not send")
    path_only = OutboundItem(chat_id=CHAT, kind="document", file_path=secret)
    with pytest.raises(DeliveryError) as info:
        run(transport.send(path_only))
    assert info.value.permanent and "no file content" in str(info.value)
    assert bot.named("send_document") == []
    with pytest.raises(DeliveryError) as info:
        run(transport.send(OutboundItem(chat_id="999", kind="text", text="hi")))
    assert info.value.permanent and "not allow-listed" in str(info.value)


def test_error_classification() -> None:
    retry = classify_error(RetryAfter(12))
    assert not retry.permanent and retry.retry_after_s == 12
    assert classify_error(BadRequest("x")).permanent
    assert classify_error(Forbidden("bot was blocked by the user")).permanent
    assert not classify_error(TimedOut()).permanent
    assert not classify_error(NetworkError("connection reset")).permanent


def test_short_flood_control_is_waited_out(transport, bot, monkeypatch) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(tg.asyncio, "sleep", fake_sleep)
    bot.fail("send_message", RetryAfter(3))
    run(transport.send(OutboundItem(chat_id=CHAT, kind="text", text="hi")))
    assert waits == [3.0] and len(bot.named("send_message")) == 2
    bot.fail("send_message", RetryAfter(600))
    with pytest.raises(DeliveryError) as info:
        run(transport.send(OutboundItem(chat_id=CHAT, kind="text", text="hi")))
    assert not info.value.permanent and info.value.retry_after_s == 600


def test_typing_indicator_repeats_while_busy(transport, bot, monkeypatch) -> None:
    monkeypatch.setattr(tg, "TYPING_INTERVAL_S", 0.01)

    async def scenario() -> None:
        await transport.set_busy(CHAT, True)
        await transport.set_busy(CHAT, True)  # idempotent
        await asyncio.sleep(0.05)
        await transport.set_busy(CHAT, False)
        count = len(bot.named("send_chat_action"))
        await asyncio.sleep(0.03)
        assert len(bot.named("send_chat_action")) == count >= 2
        assert bot.named("send_chat_action")[0] == {"chat_id": 100, "action": "typing"}

    run(scenario())


# Receiving -------------------------------------------------------------------------------------


def update(bot: FakeBot, **message: Any) -> Update:
    base = {"message_id": 10, "date": DATE, "chat": {"id": int(CHAT), "type": "private"}}
    base.update(message)
    return Update.de_json({"update_id": 1, "message": base}, bot)


def collect(transport: TelegramTransport) -> list[InboundMessage]:
    received: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        received.append(msg)

    transport.set_handler(handler)
    return received


def test_text_message(transport, bot) -> None:
    received = collect(transport)
    run(transport.handle_update(update(bot, text="How was my run?")))
    (msg,) = received
    assert (msg.transport, msg.chat_id, msg.kind, msg.text) == (
        "telegram",
        CHAT,
        "text",
        "How was my run?",
    )
    assert msg.message_id == "10" and msg.ts.isoformat() == "2030-01-02T06:00:00+00:00"


def test_messages_from_other_chats_are_dropped_without_content(transport, bot, caplog) -> None:
    received = collect(transport)
    stranger = Update.de_json(
        {
            "update_id": 2,
            "message": {
                "message_id": 1,
                "date": DATE,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 555, "is_bot": False, "first_name": "X"},
                "text": "secret content",
            },
        },
        bot,
    )
    with caplog.at_level(logging.INFO):
        run(transport.handle_update(stranger))
    assert received == []
    assert "555" in caplog.text and "not on the allow-list" in caplog.text
    assert "secret content" not in caplog.text


def test_photo_voice_document_are_saved(transport, bot, tmp_path) -> None:
    received = collect(transport)
    photo = [
        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90, "file_size": 10},
        {"file_id": "big", "file_unique_id": "b", "width": 900, "height": 900, "file_size": 99},
    ]
    run(transport.handle_update(update(bot, photo=photo, caption="my watch")))
    voice = {"file_id": "v1", "file_unique_id": "v", "duration": 12, "mime_type": "audio/ogg"}
    run(transport.handle_update(update(bot, message_id=11, voice=voice)))
    doc = {"file_id": "d1", "file_unique_id": "d", "file_name": "../../plan (v2).pdf"}
    run(transport.handle_update(update(bot, message_id=12, document=doc)))
    huge = {"file_id": "d2", "file_unique_id": "e", "file_name": "big.zip", "file_size": 30_000_000}
    run(transport.handle_update(update(bot, message_id=13, document=huge)))

    photo_msg, voice_msg, doc_msg, huge_msg = received
    inbox = tmp_path / "inbox" / "2030-01-02"
    assert photo_msg.kind == "photo" and photo_msg.text == "my watch"
    assert photo_msg.attachments[0].path == inbox / "10-photo.jpg"
    assert (inbox / "10-photo.jpg").read_bytes() == b"content of big"
    assert voice_msg.kind == "voice" and voice_msg.attachments[0].duration_s == 12
    assert (inbox / "11-voice.ogg").exists()
    assert doc_msg.attachments[0].path == inbox / "12-plan_v2_.pdf"  # no path tricks
    assert huge_msg.attachments[0].path is None
    assert "20 MB" in (huge_msg.attachments[0].error or "")
    assert [kw["file_id"] for kw in bot.named("get_file")] == ["big", "v1", "d1"]


def test_saving_never_follows_links_the_agent_planted(transport, bot, tmp_path) -> None:
    received = collect(transport)
    victim = tmp_path / "bridge-home-file"
    victim.write_text("bridge only")
    day = tmp_path / "inbox" / "2030-01-02"
    day.mkdir(parents=True)
    (day / "10-photo.jpg").symlink_to(victim)  # the name the next photo would get
    photo = [{"file_id": "p", "file_unique_id": "p", "width": 9, "height": 9, "file_size": 9}]
    run(transport.handle_update(update(bot, photo=photo)))
    assert victim.read_text() == "bridge only"
    assert received[0].attachments[0].path == day / "10-2-photo.jpg"
    assert (day / "10-2-photo.jpg").read_bytes() == b"content of p"

    # The whole day directory replaced by a link: nothing is saved, the agent hears why.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("10-photo.jpg", "10-2-photo.jpg"):
        (day / name).unlink()
    day.rmdir()
    day.symlink_to(elsewhere)
    run(transport.handle_update(update(bot, message_id=11, photo=photo)))
    attachment = received[1].attachments[0]
    assert attachment.path is None and "symbolic link" in (attachment.error or "")
    assert list(elsewhere.iterdir()) == []


def test_unsupported_messages_are_ignored(transport, bot) -> None:
    received = collect(transport)
    sticker = {
        "file_id": "st",
        "file_unique_id": "st",
        "width": 1,
        "height": 1,
        "is_animated": False,
        "is_video": False,
        "type": "regular",
    }
    run(transport.handle_update(update(bot, sticker=sticker)))
    assert received == []


def callback(bot: FakeBot, data: str) -> Update:
    keyboard = [
        [{"text": "Easy", "callback_data": "c|5|0"}, {"text": "Rest", "callback_data": "c|5|1"}]
    ]
    return Update.de_json(
        {
            "update_id": 3,
            "callback_query": {
                "id": "q1",
                "from": {"id": 100, "is_bot": False, "first_name": "A"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": 9,
                    "date": DATE,
                    "chat": {"id": int(CHAT), "type": "private"},
                    "text": "Tomorrow?",
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            },
        },
        bot,
    )


def test_button_press_becomes_a_choice_answer(transport, bot) -> None:
    received = collect(transport)
    run(transport.handle_update(callback(bot, "c|5|1")))
    (msg,) = received
    assert (msg.kind, msg.text, msg.reply_to, msg.choice_index) == ("choice_answer", "Rest", "5", 1)
    assert len(bot.named("answer_callback_query")) == 1
    (edit,) = bot.named("edit_message_reply_markup")
    (button,) = edit["reply_markup"].inline_keyboard[0]
    assert button.text == "✓ Rest" and button.callback_data == "noop"
    # Pressing the marked button again does nothing.
    run(transport.handle_update(callback(bot, "noop")))
    assert len(received) == 1 and len(bot.named("answer_callback_query")) == 2


def test_handler_errors_do_not_escape(transport, bot) -> None:
    async def broken(msg: InboundMessage) -> None:
        raise RuntimeError("bridge bug")

    transport.set_handler(broken)
    run(transport.handle_update(update(bot, text="hi")))  # logged, not raised


def test_needs_an_allow_list(tmp_path) -> None:
    with pytest.raises(ValueError):
        TelegramTransport("123:abc", [], tmp_path)
