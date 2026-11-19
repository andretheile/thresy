"""An in-memory transport for tests.

    transport = FakeTransport(chat_ids=("100",))
    await transport.start(handler)
    await transport.receive(InboundMessage(transport="fake", chat_id="100", kind="text", text="Hi"))
    transport.sent          # list of SentRecord, in delivery order
    transport.busy_log      # [(chat_id, True), (chat_id, False), ...]

Failures can be scripted per call with ``fail_next(DeliveryError(...))``.
"""

from __future__ import annotations

import itertools
import logging
from collections import deque
from dataclasses import dataclass, field

from .base import DeliveryError, InboundMessage, MessageHandler, OutboundItem

log = logging.getLogger(__name__)


@dataclass
class SentRecord:
    item: OutboundItem
    message_id: str


@dataclass
class FakeTransport:
    chat_ids: tuple[str, ...] = ("100",)
    name: str = "fake"
    sent: list[SentRecord] = field(default_factory=list)
    busy_log: list[tuple[str, bool]] = field(default_factory=list)
    dropped: list[InboundMessage] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    _handler: MessageHandler | None = None
    _failures: deque[DeliveryError | None] = field(default_factory=deque)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _busy: dict[str, bool] = field(default_factory=dict)

    @property
    def default_chat_id(self) -> str | None:
        return self.chat_ids[0] if self.chat_ids else None

    def allows(self, chat_id: str) -> bool:
        return str(chat_id) in self.chat_ids

    async def start(self, on_message: MessageHandler) -> None:
        self._handler = on_message
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self._handler = None

    def fail_next(self, *errors: DeliveryError | None) -> None:
        """Make the next ``send`` calls fail with these errors (``None``: succeed)."""
        self._failures.extend(errors)

    async def send(self, item: OutboundItem) -> str:
        if not self.allows(item.chat_id):
            raise DeliveryError(f"chat {item.chat_id} is not allow-listed", permanent=True)
        if self._failures:
            error = self._failures.popleft()
            if error is not None:
                raise error
        message_id = f"m{next(self._ids)}"
        self.sent.append(SentRecord(item=item, message_id=message_id))
        return message_id

    async def set_busy(self, chat_id: str, busy: bool) -> None:
        if self._busy.get(chat_id, False) != busy:
            self.busy_log.append((chat_id, busy))
        self._busy[chat_id] = busy

    def is_busy(self, chat_id: str) -> bool:
        return self._busy.get(chat_id, False)

    async def receive(self, message: InboundMessage) -> None:
        """Simulate an incoming message (dropped unless the chat is allow-listed)."""
        if not self.allows(message.chat_id):
            log.info("fake transport: dropped a message from a chat that is not allow-listed")
            self.dropped.append(message)
            return
        if self._handler is None:
            raise RuntimeError("transport not started")
        await self._handler(message)

    # Convenience for assertions -----------------------------------------------------------

    @property
    def texts(self) -> list[str]:
        return [r.item.text or "" for r in self.sent]
