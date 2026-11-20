"""Delivering ``outbox`` rows: in id order, once, with failures recorded.

Every outgoing message goes through the outbox: what the agent queues with ``coach send``,
the agent's final answers, and the bridge's own messages (command replies, notifications,
error messages). A single deliverer sends unsent rows in id order; a row is marked sent (and
logged in ``messages``) in one transaction right after the transport accepted it, so rows
that were delivered are never sent again after a restart.

* A permanent failure (invalid file, rejected request, unknown chat) is written to the row's
  ``error`` and the next row follows.
* A transient failure (network, flood control) stops the pass so the order is kept; the row is
  retried with backoff, and marked failed after ``COACH_BRIDGE_SEND_ATTEMPTS`` attempts.

Rows can also be written by the agent with plain SQL, so everything is checked again here,
and the transport refuses chats that are not allow-listed. Files must lie under the
workspace's ``out/``. The agent can swap a file for a link at any moment, so the bridge reads
it exactly once, without following links anywhere on the way (:func:`read_outbox_file`), and
hands the transport the bytes it read, never the path to read again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from thresy.tools.send import OUT_DIR_NAME
from thresy.transport.base import (
    MAX_CHOICE_OPTIONS,
    MAX_DOCUMENT_BYTES,
    MAX_OPTION_LENGTH,
    MAX_PHOTO_BYTES,
    OUTBOUND_KINDS,
    DeliveryError,
    OutboundItem,
    Transport,
)
from thresy.transport.files import FileRefused, read_file_under

from .clock import Clock
from .repo import Repo, Row
from .settings import BridgeSettings

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 300.0


def read_outbox_file(raw: str, workspace: Path, *, kind: str) -> tuple[Path, bytes]:
    """The path (as named) and content of an outbox file, or a permanent DeliveryError.

    ``raw`` is the row's ``file_path``: absolute, or relative to ``workspace``. It must name a
    file below ``<workspace>/out/`` without ``..``; it is opened component by component with
    ``O_NOFOLLOW`` and checked on the open file (see :func:`read_file_under`).
    """
    root = workspace / OUT_DIR_NAME
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = ()
    if not parts or ".." in parts:
        raise DeliveryError(f"{kind} files must be under {root}/ (got {raw})", permanent=True)
    limit = MAX_PHOTO_BYTES if kind == "photo" else MAX_DOCUMENT_BYTES
    try:
        data = read_file_under(root, parts, max_bytes=limit)
    except FileRefused as exc:
        raise DeliveryError(f"refused: {exc}", permanent=True) from exc
    return path, data


class OutboxDeliverer:
    def __init__(
        self,
        *,
        repo: Repo,
        transport: Transport,
        workspace: Path,
        clock: Clock,
        bridge_settings: BridgeSettings,
    ) -> None:
        self.repo = repo
        self.transport = transport
        self.workspace = workspace
        self.clock = clock
        self.bs = bridge_settings
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._attempts: dict[int, int] = {}
        self._retry_at = None

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox delivery pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.bs.bridge_outbox_poll_s)
            self._wake.clear()

    async def deliver_pending(self) -> int:
        """Deliver unsent rows in order until none are left or a transient failure occurs."""
        async with self._lock:
            if self._retry_at is not None and self.clock.now() < self._retry_at:
                return 0
            self._retry_at = None
            delivered = 0
            while True:
                rows = await self.repo.pending_outbox()
                if not rows:
                    return delivered
                for row in rows:
                    sent = await self._deliver(row)
                    if sent is None:
                        return delivered
                    delivered += sent

    def _chat_for(self, row: Row) -> str | None:
        return row.get("chat_id") or row.get("run_chat_id") or self.transport.default_chat_id

    async def _item(self, row: Row, chat_id: str) -> OutboundItem:
        kind = row["kind"]
        if kind not in OUTBOUND_KINDS:
            raise DeliveryError(f"unknown kind {kind!r}", permanent=True)
        text = row.get("text")
        if kind in ("text", "choice") and not (text or "").strip():
            raise DeliveryError("empty text", permanent=True)
        file_path: Path | None = None
        data: bytes | None = None
        if kind in ("photo", "document"):
            if not row.get("file_path"):
                raise DeliveryError("no file_path", permanent=True)
            file_path, data = await asyncio.to_thread(
                read_outbox_file, row["file_path"], self.workspace, kind=kind
            )
        options: tuple[str, ...] = ()
        if kind == "choice":
            raw = row.get("options")
            if (
                not isinstance(raw, list)
                or not 1 <= len(raw) <= MAX_CHOICE_OPTIONS
                or not all(isinstance(o, str) and 0 < len(o) <= MAX_OPTION_LENGTH for o in raw)
            ):
                raise DeliveryError("invalid options for a choice", permanent=True)
            options = tuple(raw)
        return OutboundItem(
            chat_id=chat_id,
            kind=kind,
            text=text,
            file_path=file_path,
            data=data,
            options=options,
            ref=str(row["id"]),
        )

    async def _deliver(self, row: Row) -> int | None:
        """1 if sent, 0 if marked failed, None if the pass should stop (transient failure)."""
        row_id = int(row["id"])
        chat_id = self._chat_for(row)
        try:
            if not chat_id:
                raise DeliveryError("no chat to deliver to", permanent=True)
            item = await self._item(row, chat_id)
            message_id = await self.transport.send(item)
        except DeliveryError as exc:
            return await self._failure(row_id, str(exc), exc.permanent, exc.retry_after_s)
        except Exception as exc:  # a transport bug: retry a few times, then give up on the row
            log.exception("outbox %s: unexpected delivery error", row_id)
            return await self._failure(row_id, f"internal error: {exc!r}", False, None)
        attachments: dict[str, Any] = {}
        if item.file_path is not None:
            attachments["file_path"] = str(item.file_path)
        if item.options:
            attachments["options"] = list(item.options)
        await self.repo.mark_sent(
            row,
            chat_id=chat_id,
            transport=self.transport.name,
            transport_msg_id=message_id,
            attachments=attachments or None,
        )
        self._attempts.pop(row_id, None)
        return 1

    async def _failure(
        self, row_id: int, error: str, permanent: bool, retry_after_s: float | None
    ) -> int | None:
        if not permanent:
            attempts = self._attempts.get(row_id, 0) + 1
            self._attempts[row_id] = attempts
            if attempts < self.bs.bridge_send_attempts:
                backoff = retry_after_s or min(MAX_BACKOFF_S, 5.0 * 2 ** (attempts - 1))
                self._retry_at = self.clock.now() + timedelta(seconds=backoff)
                log.warning(
                    "outbox %s: delivery failed (%s); attempt %d, retry in %.0f s",
                    row_id,
                    error,
                    attempts,
                    backoff,
                )
                return None
            error = f"gave up after {attempts} attempts: {error}"
        log.error("outbox %s: not delivered: %s", row_id, error)
        await self.repo.mark_failed(row_id, error)
        self._attempts.pop(row_id, None)
        return 0
