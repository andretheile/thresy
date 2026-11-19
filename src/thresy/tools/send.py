"""``coach send``: queue a message for the athlete in the ``outbox`` table.

The agent runs this with its own database role (``coach_agent``, which may INSERT into
``outbox`` and nothing else there). The bridge delivers the rows in order and marks them sent.
The current run's id comes from ``COACH_RUN_ID`` (set by the bridge for every agent run), so
the bridge knows which chat a row belongs to.

Files may only come from ``$COACH_WORKSPACE_DIR/out/`` (after resolving symlinks). The bridge
checks the same rule again before delivering, because the agent could also insert rows with
plain SQL.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from thresy.transport.base import (
    MAX_CHOICE_OPTIONS,
    MAX_DOCUMENT_BYTES,
    MAX_OPTION_LENGTH,
    MAX_PHOTO_BYTES,
    MAX_TEXT_LENGTH,
)

OUT_DIR_NAME = "out"


class SendError(ValueError):
    """The request is invalid; the message says what to do instead."""


@dataclass(frozen=True)
class Queued:
    id: int
    kind: str
    run_id: int | None
    file_path: str | None = None


def out_dir(workspace: Path) -> Path:
    return (workspace / OUT_DIR_NAME).resolve()


def check_file(path: str | Path, workspace: Path, *, kind: str, cwd: Path | None = None) -> Path:
    """Resolve ``path`` (symlinks included) and make sure it is a file under ``workspace/out``.

    Relative paths are taken relative to ``cwd`` (default: the current directory). Returns the
    resolved absolute path; raises :class:`SendError` otherwise.
    """
    allowed = out_dir(workspace)
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = (cwd or Path.cwd()) / raw
    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SendError(f"file not found: {raw}") from exc
    except (OSError, RuntimeError) as exc:  # symlink loops, permissions
        raise SendError(f"cannot read {raw}: {exc}") from exc
    if not resolved.is_relative_to(allowed):
        raise SendError(
            f"{kind} files must be under {allowed}/ (got {resolved}); save the file there first"
        )
    if not resolved.is_file():
        raise SendError(f"not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise SendError(f"file is empty: {resolved}")
    limit = MAX_PHOTO_BYTES if kind == "photo" else MAX_DOCUMENT_BYTES
    if size > limit:
        raise SendError(
            f"{resolved.name} is {size / 1e6:.1f} MB; the limit for a {kind} is "
            f"{limit // (1024 * 1024)} MB"
        )
    return resolved


def check_text(text: str | None, *, required: bool, what: str = "text") -> str | None:
    if text is None or not text.strip():
        if required:
            raise SendError(f"{what} must not be empty")
        return None
    if len(text) > MAX_TEXT_LENGTH:
        raise SendError(f"{what} is {len(text)} characters; the limit is {MAX_TEXT_LENGTH}")
    return text


def check_options(options: Sequence[str]) -> list[str]:
    cleaned = [o.strip() for o in options]
    if not cleaned:
        raise SendError("a choice needs at least one --option")
    if len(cleaned) > MAX_CHOICE_OPTIONS:
        raise SendError(f"at most {MAX_CHOICE_OPTIONS} options")
    for option in cleaned:
        if not option:
            raise SendError("options must not be empty")
        if len(option) > MAX_OPTION_LENGTH:
            raise SendError(f"option {option!r} is longer than {MAX_OPTION_LENGTH} characters")
    if len(set(cleaned)) != len(cleaned):
        raise SendError("options must be different from each other")
    return cleaned


def run_id_from_env(environ: dict[str, str] | None = None) -> int | None:
    value = (environ if environ is not None else os.environ).get("COACH_RUN_ID", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise SendError(f"COACH_RUN_ID is not a number: {value!r}") from exc


def queue(
    conn: Any,
    *,
    kind: str,
    text: str | None = None,
    file_path: Path | None = None,
    options: Sequence[str] | None = None,
    run_id: int | None = None,
) -> Queued:
    """Insert one outbox row (kind, text, file, options, run) and return its id."""
    row = conn.execute(
        "INSERT INTO outbox (kind, text, file_path, options, run_id)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            kind,
            text,
            str(file_path) if file_path is not None else None,
            Jsonb(list(options)) if options is not None else None,
            run_id,
        ),
    ).fetchone()
    row_id = row["id"] if isinstance(row, dict) else row[0]
    return Queued(
        id=int(row_id),
        kind=kind,
        run_id=run_id,
        file_path=str(file_path) if file_path is not None else None,
    )
