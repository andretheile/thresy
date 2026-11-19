"""The transport protocol and the message types every transport speaks.

A transport connects the bridge to one chat system (Telegram now, a private web page later).
It knows nothing about agents, sessions or the database: it turns the chat system's updates
into :class:`InboundMessage` objects, delivers :class:`OutboundItem` objects, and shows a
"busy" indicator. Nothing outside the transport's own package may know which chat system is
behind it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

OutboundKind = Literal["text", "photo", "document", "choice"]
InboundKind = Literal["text", "photo", "document", "voice", "audio", "choice_answer"]
AttachmentKind = Literal["photo", "document", "voice", "audio"]

OUTBOUND_KINDS: tuple[str, ...] = ("text", "photo", "document", "choice")

# Limits every transport supports (stricter chat-system limits are the transport's business).
MAX_CHOICE_OPTIONS = 10
MAX_OPTION_LENGTH = 64
MAX_TEXT_LENGTH = 20_000
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class Attachment:
    """A file the athlete sent, saved by the transport (usually under the workspace)."""

    kind: AttachmentKind
    path: Path | None
    """Where the file was saved; None if it could not be saved (see ``error``)."""
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    duration_s: float | None = None
    """Length of a voice or audio message."""
    error: str | None = None

    def as_json(self, root: Path | None = None) -> dict[str, Any]:
        """A JSON-friendly dict; ``path`` relative to ``root`` when it lies below it."""
        path: str | None = None
        if self.path is not None:
            path = str(self.path)
            if root is not None:
                with contextlib.suppress(ValueError):
                    path = str(self.path.resolve().relative_to(root.resolve()))
        data: dict[str, Any] = {"kind": self.kind, "path": path}
        for key in ("file_name", "mime_type", "size", "duration_s", "error"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class InboundMessage:
    """Something the athlete sent: a text, a file, a voice message or a button press."""

    transport: str
    chat_id: str
    kind: InboundKind
    text: str = ""
    """The text, the caption of a file, or the label of the pressed button."""
    message_id: str | None = None
    """The transport's own id of the message (for the log)."""
    ts: datetime | None = None
    attachments: tuple[Attachment, ...] = ()
    reply_to: str | None = None
    """For ``choice_answer``: the ``OutboundItem.ref`` of the question that was answered."""
    choice_index: int | None = None
    """For ``choice_answer``: the 0-based index of the pressed option."""


@dataclass(frozen=True)
class OutboundItem:
    """Something to deliver to a chat. ``text`` is Markdown; the transport renders it."""

    chat_id: str
    kind: OutboundKind
    text: str | None = None
    """Message text (``text``, ``choice``) or caption (``photo``, ``document``)."""
    file_path: Path | None = None
    options: tuple[str, ...] = ()
    """Button labels of a ``choice``."""
    ref: str | None = None
    """The bridge's reference (the outbox id). A transport echoes it back as
    ``InboundMessage.reply_to`` when a button of this item is pressed."""


class DeliveryError(Exception):
    """A transport could not deliver an item.

    ``permanent``: retrying the same item will not help (invalid chat, rejected file, ...).
    Otherwise the failure is transient (network, flood control) and the caller may retry,
    after ``retry_after_s`` seconds if given.
    """

    def __init__(
        self, message: str, *, permanent: bool, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after_s = retry_after_s


MessageHandler = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class Transport(Protocol):
    """What the bridge needs from a chat system.

    ``start`` begins receiving and returns once that is running; ``on_message`` is awaited for
    every accepted message, so it should return quickly. Messages from chats that ``allows``
    rejects are dropped (and logged without content) before they reach ``on_message``.
    ``send`` raises :class:`DeliveryError`. ``set_busy`` must not raise.
    """

    name: str

    @property
    def default_chat_id(self) -> str | None:
        """The chat that proactive messages go to (the first allow-listed one)."""
        ...

    def allows(self, chat_id: str) -> bool: ...

    async def start(self, on_message: MessageHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, item: OutboundItem) -> str:
        """Deliver ``item``; returns the transport's message id (several, comma separated, if
        the item had to be split into more than one message)."""
        ...

    async def set_busy(self, chat_id: str, busy: bool) -> None: ...
