"""Chat transports: the protocol, its message types and a fake for tests.

The Telegram implementation lives in :mod:`thresy.transport.telegram`, the only module that
knows about Telegram. Import it only where a Telegram transport is actually built (it imports
python-telegram-bot).
"""

from .base import (
    MAX_CHOICE_OPTIONS,
    MAX_DOCUMENT_BYTES,
    MAX_OPTION_LENGTH,
    MAX_PHOTO_BYTES,
    MAX_TEXT_LENGTH,
    OUTBOUND_KINDS,
    Attachment,
    DeliveryError,
    InboundMessage,
    MessageHandler,
    OutboundItem,
    Transport,
)
from .fake import FakeTransport, SentRecord

__all__ = [
    "MAX_CHOICE_OPTIONS",
    "MAX_DOCUMENT_BYTES",
    "MAX_OPTION_LENGTH",
    "MAX_PHOTO_BYTES",
    "MAX_TEXT_LENGTH",
    "OUTBOUND_KINDS",
    "Attachment",
    "DeliveryError",
    "FakeTransport",
    "InboundMessage",
    "MessageHandler",
    "OutboundItem",
    "SentRecord",
    "Transport",
]
