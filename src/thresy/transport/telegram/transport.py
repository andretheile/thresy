"""The Telegram transport (python-telegram-bot, long polling: no port is opened).

Inbound: text, photos, documents, voice and audio messages, and presses of the buttons of a
``choice``. Files are saved under ``inbox_dir`` (the workspace's ``inbox/``) so the agent can
read them; the agent can change that directory, so files are created without following links
(:mod:`thresy.transport.files`). Updates from chats that are not allow-listed are dropped and
logged without content (the chat id is logged so the owner can find it when setting the bot
up).

Outbound: Markdown is rendered to text plus entities (see :mod:`.markdown`), split under the
size limit, and sent without entities if Telegram rejects them. Files are sent from
``OutboundItem.data``, the bytes the bridge read; the transport never opens a file path the
agent could have swapped. Photos that Telegram refuses as photos are sent as documents. A
``choice`` gets one inline button per option; a pressed button is replaced by a single
"✓ <label>" button so it cannot be pressed twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import warnings
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageEntity,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler
from telegram.warnings import PTBDeprecationWarning

from thresy.transport.base import (
    MAX_DOCUMENT_BYTES,
    MAX_PHOTO_BYTES,
    Attachment,
    DeliveryError,
    InboundMessage,
    MessageHandler,
    OutboundItem,
)
from thresy.transport.files import FileRefused, create_file_under

from .markdown import CAPTION_LIMIT, Rich, format_markdown, plain, utf16_len

log = logging.getLogger(__name__)

DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024  # Bot API getFile limit
TYPING_INTERVAL_S = 4.0  # a chat action lasts about 5 s
CHOICE_PREFIX = "c"
NOOP_DATA = "noop"
MAX_INLINE_RETRY_AFTER_S = 30.0


def _safe_name(name: str, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
    return (cleaned or default)[:80]


def _seconds(value: Any) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0


def _period(obj: Any, attr: str) -> Any:
    """Read a time-period attribute (int or timedelta, depending on PTB_TIMEDELTA) without
    python-telegram-bot's deprecation warning about the int form."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        return getattr(obj, attr)


def _retry_after_s(exc: RetryAfter) -> float:
    return _seconds(_period(exc, "retry_after"))


def classify_error(exc: TelegramError) -> DeliveryError:
    """Map a Telegram error to a permanent or transient :class:`DeliveryError`."""
    if isinstance(exc, RetryAfter):
        return DeliveryError(str(exc), permanent=False, retry_after_s=_retry_after_s(exc))
    if isinstance(exc, BadRequest | Forbidden | InvalidToken | ChatMigrated):
        return DeliveryError(f"{type(exc).__name__}: {exc}", permanent=True)
    if isinstance(exc, NetworkError):  # includes TimedOut
        return DeliveryError(f"{type(exc).__name__}: {exc}", permanent=False)
    return DeliveryError(f"{type(exc).__name__}: {exc}", permanent=False)


class TelegramTransport:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_ids: Sequence[str],
        inbox_dir: Path,
        *,
        bot: Any | None = None,
    ) -> None:
        """``bot`` replaces the real :class:`telegram.Bot` (tests pass a recording fake)."""
        if not chat_ids:
            raise ValueError("no allow-listed chat ids (COACH_TELEGRAM_CHAT_IDS)")
        self._token = token
        self._chat_ids = tuple(str(c).strip() for c in chat_ids)
        self.inbox_dir = inbox_dir
        self._bot: Any = bot
        self._app: Application | None = None
        self._handler: MessageHandler | None = None
        self._typing: dict[str, asyncio.Task[None]] = {}

    # -- protocol ------------------------------------------------------------------------------

    @property
    def default_chat_id(self) -> str | None:
        return self._chat_ids[0]

    def allows(self, chat_id: str) -> bool:
        return str(chat_id) in self._chat_ids

    @property
    def bot(self) -> Any:
        if self._bot is None:
            self._bot = Bot(self._token)
        return self._bot

    async def start(self, on_message: MessageHandler) -> None:
        self._handler = on_message
        app = ApplicationBuilder().token(self._token).build()
        app.add_handler(TypeHandler(Update, self._on_update))
        app.add_error_handler(self._on_error)
        self._app = app
        self._bot = app.bot
        await app.initialize()
        assert app.updater is not None
        await app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            bootstrap_retries=3,
            error_callback=self._on_polling_error,
        )
        await app.start()
        log.info("telegram: polling started for %d allow-listed chat(s)", len(self._chat_ids))

    async def stop(self) -> None:
        for task in list(self._typing.values()):
            task.cancel()
        self._typing.clear()
        app, self._app = self._app, None
        if app is None:
            return
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("telegram: error while stopping")

    async def set_busy(self, chat_id: str, busy: bool) -> None:
        chat_id = str(chat_id)
        if busy:
            task = self._typing.get(chat_id)
            if task is None or task.done():
                self._typing[chat_id] = asyncio.create_task(
                    self._typing_loop(chat_id), name=f"telegram-typing:{chat_id}"
                )
        else:
            task = self._typing.pop(chat_id, None)
            if task is not None:
                task.cancel()

    async def send(self, item: OutboundItem) -> str:
        if not self.allows(item.chat_id):
            raise DeliveryError(f"chat {item.chat_id} is not allow-listed", permanent=True)
        chat = int(item.chat_id)
        try:
            if item.kind == "text":
                ids = await self._send_text(chat, item.text or "")
            elif item.kind == "choice":
                ids = await self._send_text(chat, item.text or "", markup=self._keyboard(item))
            elif item.kind in ("photo", "document"):
                ids = await self._send_file(chat, item)
            else:
                raise DeliveryError(f"unsupported kind {item.kind!r}", permanent=True)
        except TelegramError as exc:
            raise classify_error(exc) from exc
        return ",".join(ids)

    # -- sending ---------------------------------------------------------------------------------

    async def _call(self, method: str, **kwargs: Any) -> Any:
        """Call a Bot method, waiting once for short flood-control pauses."""
        try:
            return await getattr(self.bot, method)(**kwargs)
        except RetryAfter as exc:
            wait = _retry_after_s(exc)
            if wait > MAX_INLINE_RETRY_AFTER_S:
                raise
            await asyncio.sleep(wait)
            return await getattr(self.bot, method)(**kwargs)

    @staticmethod
    def _entities(chunk: Rich) -> list[MessageEntity] | None:
        if not chunk.entities:
            return None
        return [
            MessageEntity(e.type, e.offset, e.length, url=e.url, language=e.language)
            for e in chunk.entities
        ]

    async def _send_chunk(self, chat: int, chunk: Rich, markup: Any = None) -> str:
        kwargs: dict[str, Any] = {
            "chat_id": chat,
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        if markup is not None:
            kwargs["reply_markup"] = markup
        entities = self._entities(chunk)
        try:
            msg = await self._call("send_message", text=chunk.text, entities=entities, **kwargs)
        except BadRequest as exc:
            if entities is None:
                raise
            log.warning("telegram rejected the formatting (%s); sending plain text", exc)
            msg = await self._call("send_message", text=plain(chunk).text, **kwargs)
        return str(msg.message_id)

    async def _send_text(self, chat: int, text: str, markup: Any = None) -> list[str]:
        chunks = format_markdown(text)
        if not chunks:
            if markup is None:
                raise DeliveryError("empty message", permanent=True)
            chunks = [Rich("…")]
        ids = []
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            ids.append(await self._send_chunk(chat, chunk, markup if last else None))
        return ids

    def _keyboard(self, item: OutboundItem) -> InlineKeyboardMarkup:
        if not item.options:
            raise DeliveryError("a choice needs at least one option", permanent=True)
        buttons = [
            InlineKeyboardButton(label, callback_data=f"{CHOICE_PREFIX}|{item.ref or ''}|{i}")
            for i, label in enumerate(item.options)
        ]
        short = all(len(label) <= 16 for label in item.options)
        per_row = 2 if short and len(buttons) > 1 else 1
        rows = [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]
        return InlineKeyboardMarkup(rows)

    async def _send_file(self, chat: int, item: OutboundItem) -> list[str]:
        # Only the bytes the bridge read: the file at item.file_path may have been swapped.
        path, data = item.file_path, item.data
        if path is None or not data:
            raise DeliveryError("no file content given (the bridge reads files)", permanent=True)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise DeliveryError(f"{path.name} is larger than 50 MB", permanent=True)
        chunks = format_markdown(item.text or "") if (item.text or "").strip() else []
        caption: Rich | None = None
        if len(chunks) == 1 and utf16_len(chunks[0].text) <= CAPTION_LIMIT:
            caption, chunks = chunks[0], []
        as_photo = item.kind == "photo" and len(data) <= MAX_PHOTO_BYTES
        message_id = None
        if as_photo:
            try:
                message_id = await self._send_media(chat, "photo", data, path.name, caption)
            except BadRequest as exc:
                log.warning("telegram refused %s as a photo (%s); sending a document", path, exc)
        if message_id is None:
            message_id = await self._send_media(chat, "document", data, path.name, caption)
        ids = [message_id]
        for chunk in chunks:
            ids.append(await self._send_chunk(chat, chunk))
        return ids

    async def _send_media(
        self, chat: int, kind: str, data: bytes, name: str, caption: Rich | None
    ) -> str:
        kwargs: dict[str, Any] = {"chat_id": chat, kind: data, "filename": name}
        if caption is not None:
            kwargs["caption"] = caption.text
            kwargs["caption_entities"] = self._entities(caption)
        try:
            msg = await self._call(f"send_{kind}", **kwargs)
        except BadRequest as exc:
            if caption is None or not caption.entities or "entit" not in str(exc).lower():
                raise
            kwargs["caption_entities"] = None
            msg = await self._call(f"send_{kind}", **kwargs)
        return str(msg.message_id)

    async def _typing_loop(self, chat_id: str) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("telegram: typing indicator failed: %s", exc)
            await asyncio.sleep(TYPING_INTERVAL_S)

    # -- receiving -------------------------------------------------------------------------------

    def _on_polling_error(self, error: TelegramError) -> None:
        log.warning("telegram: polling error: %s", error)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram: error while handling an update", exc_info=context.error)

    async def _on_update(self, update: Update, context: Any = None) -> None:
        await self.handle_update(update)

    async def handle_update(self, update: Update) -> None:
        """Turn one Telegram update into an InboundMessage (public for tests)."""
        chat = update.effective_chat
        if chat is None:
            return
        chat_id = str(chat.id)
        if not self.allows(chat_id):
            user = update.effective_user
            log.warning(
                "telegram: dropped an update from chat %s (user %s): not on the allow-list",
                chat_id,
                user.id if user else "?",
            )
            return
        if update.callback_query is not None:
            inbound = await self._from_callback(update, chat_id)
        elif update.message is not None:
            inbound = await self._from_message(update.message, chat_id)
        else:
            return
        if inbound is None or self._handler is None:
            return
        try:
            await self._handler(inbound)
        except Exception:
            log.exception("telegram: the message handler failed")

    def set_handler(self, on_message: MessageHandler) -> None:
        """Set the handler without starting to poll (tests)."""
        self._handler = on_message

    async def _from_callback(self, update: Update, chat_id: str) -> InboundMessage | None:
        query = update.callback_query
        assert query is not None
        data = query.data or ""
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != CHOICE_PREFIX:
            await self._answer(query)
            return None
        ref = parts[1] or None
        try:
            index = int(parts[2])
        except ValueError:
            await self._answer(query)
            return None
        label = ""
        message = query.message
        markup = getattr(message, "reply_markup", None)
        if markup is not None:
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data == data:
                        label = button.text
        await self._answer(query)
        if label:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(f"✓ {label}", callback_data=NOOP_DATA)]]
                    )
                )
            except TelegramError as exc:
                log.debug("telegram: could not mark the pressed button: %s", exc)
        return InboundMessage(
            transport=self.name,
            chat_id=chat_id,
            kind="choice_answer",
            text=label,
            message_id=str(message.message_id) if message is not None else None,
            ts=datetime.now(UTC),
            reply_to=ref,
            choice_index=index,
        )

    @staticmethod
    async def _answer(query: Any) -> None:
        try:
            await query.answer()
        except TelegramError as exc:
            log.debug("telegram: answering a callback query failed: %s", exc)

    async def _from_message(self, message: Any, chat_id: str) -> InboundMessage | None:
        ts = message.date or datetime.now(UTC)
        base = {
            "transport": self.name,
            "chat_id": chat_id,
            "message_id": str(message.message_id),
            "ts": ts,
        }
        if message.text is not None:
            return InboundMessage(kind="text", text=message.text, **base)
        caption = message.caption or ""
        if message.photo:
            size = message.photo[-1]
            att = await self._download(
                "photo", size.file_id, size.file_size, message, "photo.jpg", "image/jpeg"
            )
            return InboundMessage(kind="photo", text=caption, attachments=(att,), **base)
        if message.document is not None:
            doc = message.document
            att = await self._download(
                "document",
                doc.file_id,
                doc.file_size,
                message,
                doc.file_name or "document",
                doc.mime_type,
            )
            return InboundMessage(kind="document", text=caption, attachments=(att,), **base)
        if message.voice is not None:
            voice = message.voice
            att = await self._download(
                "voice",
                voice.file_id,
                voice.file_size,
                message,
                "voice.ogg",
                voice.mime_type,
                duration=_period(voice, "duration"),
            )
            return InboundMessage(kind="voice", text=caption, attachments=(att,), **base)
        if message.audio is not None:
            audio = message.audio
            att = await self._download(
                "audio",
                audio.file_id,
                audio.file_size,
                message,
                audio.file_name or "audio",
                audio.mime_type,
                duration=_period(audio, "duration"),
            )
            return InboundMessage(kind="audio", text=caption, attachments=(att,), **base)
        log.info("telegram: ignored a message of an unsupported type in chat %s", chat_id)
        return None

    async def _download(
        self,
        kind: str,
        file_id: str,
        size: int | None,
        message: Any,
        name: str,
        mime: str | None,
        *,
        duration: Any = None,
    ) -> Attachment:
        safe = _safe_name(name, kind)
        when = (message.date or datetime.now(UTC)).astimezone(UTC)
        day = when.strftime("%Y-%m-%d")
        duration_s = _seconds(duration) if duration is not None else None
        common = {
            "kind": kind,
            "file_name": safe,
            "mime_type": mime,
            "size": size,
            "duration_s": duration_s,
        }
        if size is not None and size > DOWNLOAD_LIMIT_BYTES:
            error = "file larger than 20 MB (bot download limit)"
            return Attachment(path=None, error=error, **common)
        try:
            tg_file = await self.bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
        except (TelegramError, OSError) as exc:
            log.warning("telegram: could not download a %s: %s", kind, exc)
            return Attachment(path=None, error=f"download failed: {exc}", **common)
        try:
            target = await asyncio.to_thread(self._save, day, message.message_id, safe, data)
        except (FileRefused, OSError) as exc:
            log.warning("telegram: could not save a %s: %s", kind, exc)
            return Attachment(path=None, error=f"could not save the file: {exc}", **common)
        return Attachment(path=target, **common)

    def _save(self, day: str, message_id: int, safe: str, data: bytes) -> Path:
        """Write a new file under ``inbox/<day>/``. The agent can change the inbox, so no link
        is followed and nothing that exists is overwritten (a second copy gets a number)."""
        for n in range(1, 10):
            name = f"{message_id}-{safe}" if n == 1 else f"{message_id}-{n}-{safe}"
            with contextlib.suppress(FileExistsError):
                return create_file_under(self.inbox_dir, (day,), name, data)
        raise FileRefused(f"{self.inbox_dir.name}/{day}/{message_id}-{safe} exists already")
