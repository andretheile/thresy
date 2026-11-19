"""The fake transport and protocol conformance."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from thresy.transport import (
    Attachment,
    DeliveryError,
    FakeTransport,
    InboundMessage,
    OutboundItem,
    Transport,
)
from thresy.transport.telegram import TelegramTransport


def test_both_transports_implement_the_protocol(tmp_path: Path) -> None:
    assert isinstance(FakeTransport(), Transport)
    assert isinstance(TelegramTransport("1:x", ["1"], tmp_path), Transport)


def test_fake_transport_records_and_fails_on_request() -> None:
    async def scenario() -> None:
        fake = FakeTransport(chat_ids=("1",))
        received: list[InboundMessage] = []

        async def handler(msg: InboundMessage) -> None:
            received.append(msg)

        await fake.start(handler)
        await fake.receive(InboundMessage(transport="fake", chat_id="1", kind="text", text="hi"))
        await fake.receive(InboundMessage(transport="fake", chat_id="2", kind="text", text="no"))
        assert [m.text for m in received] == ["hi"] and len(fake.dropped) == 1
        fake.fail_next(DeliveryError("down", permanent=False), None)
        item = OutboundItem(chat_id="1", kind="text", text="x")
        with pytest.raises(DeliveryError):
            await fake.send(item)
        assert await fake.send(item) == "m1"
        with pytest.raises(DeliveryError):
            await fake.send(OutboundItem(chat_id="2", kind="text", text="x"))
        await fake.set_busy("1", True)
        await fake.set_busy("1", True)
        await fake.set_busy("1", False)
        assert fake.busy_log == [("1", True), ("1", False)]
        assert fake.default_chat_id == "1"

    asyncio.run(scenario())


def test_attachment_json_is_relative_to_the_workspace(tmp_path: Path) -> None:
    saved = tmp_path / "inbox" / "a.jpg"
    att = Attachment(kind="photo", path=saved, size=3)
    assert att.as_json(tmp_path) == {"kind": "photo", "path": "inbox/a.jpg", "size": 3}
    assert att.as_json(tmp_path / "elsewhere")["path"] == str(saved)
    assert Attachment(kind="voice", path=None, error="x").as_json() == {
        "kind": "voice",
        "path": None,
        "error": "x",
    }
