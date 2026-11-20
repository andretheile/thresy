"""The Telegram transport through its real HTTP stack (python-telegram-bot, httpx, httpcore)
against a fake Bot API server on 127.0.0.1: the bot token never reaches the process's output.

Logging is set up exactly as in the services (``thresy.logs.configure``), and everything the
process writes to stdout and stderr is captured at the file-descriptor level.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import pytest

from thresy.transport import DeliveryError, InboundMessage, OutboundItem
from thresy.transport.telegram import TelegramTransport

from ..logcapture import isolated_secrets, production_logging

pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")

# An obviously fake token in the shape of a real one (bot id, colon, 35 characters).
TOKEN = "987654321:FAKE-token-for-tests-only-000000000"
SECRET_PART = TOKEN.split(":")[1]
CHAT = 100
DATE = 1893564000  # 2030-01-02 06:00 UTC
PHOTO = b"\xff\xd8\xff\xe0fake-jpeg"


def _message(message_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": DATE,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": CHAT, "is_bot": False, "first_name": "Athlete"},
        **extra,
    }


@dataclass
class FakeBotApi:
    """Answers the Bot API calls the transport makes; ``failures`` scripts error answers per
    method (status, description), consumed in order; ``None`` answers normally."""

    updates: list[dict[str, Any]]
    failures: dict[str, list[tuple[int, str] | None]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def answer(self, method: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.calls.append(method)
            queue = self.failures.get(method)
            failure = queue.pop(0) if queue else None
            updates: list[dict[str, Any]] = []
            if method == "getUpdates" and failure is None:
                updates, self.updates = self.updates, []
        if failure is not None:
            status, description = failure
            return status, {"ok": False, "error_code": status, "description": description}
        result: Any = True
        if method == "getMe":
            result = {
                "id": 987654321,
                "is_bot": True,
                "first_name": "Fake",
                "username": "fake_test_bot",
            }
        elif method == "getUpdates":
            result = updates
        elif method == "sendMessage":
            result = _message(500 + len(self.calls), text="sent")
        elif method == "getFile":
            result = {
                "file_id": "big",
                "file_unique_id": "b",
                "file_size": len(PHOTO),
                "file_path": "photos/file_1.jpg",
            }
        return 200, {"ok": True, "result": result}

    def count(self, method: str) -> int:
        with self.lock:
            return self.calls.count(method)


def serve(api: FakeBotApi) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            prefix = f"/bot{TOKEN}/"
            if not self.path.startswith(prefix):
                self._reply(404, b'{"ok":false,"error_code":404,"description":"Not Found"}', "")
                return
            method = self.path[len(prefix) :]
            status, payload = api.answer(method)
            if method == "getUpdates" and not payload.get("result"):
                threading.Event().wait(0.05)  # a short long poll
            self._reply(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:
            # python-telegram-bot percent-encodes the file URL's path (the token's colon too).
            if unquote(self.path) == f"/file/bot{TOKEN}/photos/file_1.jpg":
                api.calls.append("download")
                self._reply(200, PHOTO, "image/jpeg")
            else:
                self._reply(404, b"", "text/plain")

        def log_message(self, format: str, *args: Any) -> None:  # the server's own output
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def fake_api() -> Iterator[tuple[FakeBotApi, str]]:
    api = FakeBotApi(
        updates=[
            {"update_id": 1, "message": _message(10, text="hello")},
            {
                "update_id": 2,
                "message": _message(
                    11,
                    caption="my watch",
                    photo=[
                        {
                            "file_id": "big",
                            "file_unique_id": "b",
                            "width": 9,
                            "height": 9,
                            "file_size": len(PHOTO),
                        }
                    ],
                ),
            },
        ]
    )
    server, url = serve(api)
    try:
        yield api, url
    finally:
        server.shutdown()
        server.server_close()


async def wait_for(condition, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("verbose", [False, True], ids=["info", "debug"])
def test_the_bot_token_never_reaches_the_output(fake_api, tmp_path: Path, capfd, verbose) -> None:
    api, url = fake_api
    # Errors whose text quotes the request URL, token included, as an error of the HTTP layer
    # can: a rejected formatting (logged), a refused message (DeliveryError, stored in the
    # outbox) and a failed long poll (logged by the polling error callback).
    quoted = f"see {url}/bot{TOKEN}/sendMessage"
    api.failures = {
        "sendMessage": [
            (400, f"Bad Request: can't parse entities ({quoted})"),
            None,  # the same text again without formatting
            (403, f"Forbidden: bot was blocked by the user ({quoted})"),
        ],
        "getUpdates": [None, (502, f"Bad Gateway ({url}/bot{TOKEN}/getUpdates)")],
    }
    received: list[InboundMessage] = []

    async def scenario() -> None:
        transport = TelegramTransport(TOKEN, [str(CHAT)], tmp_path / "inbox", api_url=url)

        async def on_message(msg: InboundMessage) -> None:
            received.append(msg)
            if len(received) == 1:  # a traceback that holds the token
                raise RuntimeError(f"handler failed after {transport.bot.base_url}/getUpdates")

        await transport.start(on_message)
        try:
            await wait_for(lambda: len(received) == 2)
            await wait_for(lambda: api.count("getUpdates") >= 3)  # past the failed poll
            await transport.set_busy(str(CHAT), True)
            await wait_for(lambda: api.count("sendChatAction") >= 1)
            await transport.set_busy(str(CHAT), False)
            sent = await transport.send(
                OutboundItem(chat_id=str(CHAT), kind="text", text="**Easy** run today")
            )
            assert sent
            with pytest.raises(DeliveryError) as refused:
                await transport.send(OutboundItem(chat_id=str(CHAT), kind="text", text="hi"))
            assert refused.value.permanent
            assert TOKEN not in str(refused.value) and "<redacted>" in str(refused.value)
        finally:
            await transport.stop()

    with isolated_secrets(), production_logging(verbose=verbose):
        asyncio.run(scenario())
    out, err = capfd.readouterr()
    output = out + err

    # The whole run went through the real HTTP layer ...
    assert [m.kind for m in received] == ["text", "photo"]
    assert received[1].attachments[0].path is not None  # downloaded from the file URL
    assert api.count("download") == 1 and api.count("sendMessage") == 3
    # ... it was logged ...
    assert "telegram: polling started" in output
    assert "telegram rejected the formatting" in output
    assert "telegram: polling error" in output
    assert "telegram: the message handler failed" in output and "Traceback" in output
    assert "<redacted>" in output
    # ... and neither the token nor its secret part, plain or percent-encoded, was written.
    for leaked in (TOKEN, SECRET_PART, quote(TOKEN, safe="")):
        assert leaked not in output
    # Request logging of the HTTP client is off (it wrote one line per call, with the URL).
    assert "HTTP Request:" not in output
