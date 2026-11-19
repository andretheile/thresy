"""Telegram transport. The only package that knows about Telegram (python-telegram-bot)."""

from .transport import TelegramTransport, classify_error

__all__ = ["TelegramTransport", "classify_error"]
