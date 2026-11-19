"""``agent_requests`` of kind ``sync_now``: "sync now and tell me what came in".

The agent (``coach sync request``, role ``coach_agent``: SELECT, and INSERT of ``kind`` and
``payload``) or the bridge inserts a row. The poller claims pending rows between passes, runs
one pass for all of them, and writes ``status`` (``done`` or ``failed``), ``result``
``{new_activities: [ids], sleep_synced: bool, wake_date}`` or ``error``, ``started_at``,
``finished_at`` and ``handled_by``.

``thresy.store`` has no functions for this table yet, so its SQL lives here.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from thresy.store import Conn, to_jsonb
from thresy.sync.config import SyncConfig
from thresy.sync.passes import ClientSource, SyncOutcome, never, sync_once

SYNC_NOW = "sync_now"
FINAL_STATUSES = ("done", "failed")


def handler_name() -> str:
    """``handled_by`` of this process."""
    return f"sync:{socket.gethostname()}:{os.getpid()}"


def create_request(conn: Conn, kind: str = SYNC_NOW, payload: dict[str, Any] | None = None) -> int:
    """Insert a pending request; returns its id. Works with the ``coach_agent`` role."""
    row = conn.execute(
        "INSERT INTO agent_requests (kind, payload) VALUES (%s, %s) RETURNING id",
        (kind, to_jsonb(payload or {})),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def get_request(conn: Conn, request_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, created_at, kind, status, result, error, started_at, finished_at, handled_by"
        " FROM agent_requests WHERE id = %s",
        (request_id,),
    ).fetchone()


def wait_for_request(
    conn: Conn,
    request_id: int,
    timeout_s: float,
    *,
    poll_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """Poll until the request is ``done`` or ``failed`` or ``timeout_s`` has passed; returns
    the last state of the row."""
    deadline = monotonic() + max(0.0, timeout_s)
    while True:
        row = get_request(conn, request_id)
        if row is None or row["status"] in FINAL_STATUSES:
            return row
        remaining = deadline - monotonic()
        if remaining <= 0:
            return row
        sleep(min(poll_s, remaining))


def has_pending(conn: Conn, kind: str = SYNC_NOW) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM agent_requests WHERE kind = %s AND status = 'pending')"
        " AS pending",
        (kind,),
    ).fetchone()
    return bool(row and row["pending"])


def claim_pending(conn: Conn, handled_by: str, kind: str = SYNC_NOW) -> list[int]:
    """Mark all pending requests of ``kind`` as running; returns their ids."""
    rows = conn.execute(
        "UPDATE agent_requests SET status = 'running', started_at = now(), handled_by = %s"
        " WHERE id IN (SELECT id FROM agent_requests WHERE kind = %s AND status = 'pending'"
        "              ORDER BY id FOR UPDATE SKIP LOCKED)"
        " RETURNING id",
        (handled_by, kind),
    ).fetchall()
    return sorted(int(r["id"]) for r in rows)


def finish(
    conn: Conn, ids: list[int], *, result: dict[str, Any] | None = None, error: str | None = None
) -> None:
    """Set the claimed requests to ``done`` with ``result``, or ``failed`` with ``error``."""
    if not ids:
        return
    conn.execute(
        "UPDATE agent_requests SET status = %s, result = %s, error = %s, finished_at = now()"
        " WHERE id = ANY(%s)",
        (
            "failed" if error is not None else "done",
            to_jsonb(result) if result is not None else None,
            error,
            ids,
        ),
    )


def fail_orphans(conn: Conn, kind: str = SYNC_NOW) -> int:
    """Fail requests left ``running`` by a sync process that died. Call only while holding the
    poller lock (then no other process can be working on them)."""
    rows = conn.execute(
        "UPDATE agent_requests SET status = 'failed', finished_at = now(),"
        " error = 'interrupted: the sync service stopped before answering'"
        " WHERE kind = %s AND status = 'running' RETURNING id",
        (kind,),
    ).fetchall()
    return len(rows)


def counts(conn: Conn, kind: str = SYNC_NOW) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM agent_requests"
        " WHERE kind = %s AND status IN ('pending', 'running') GROUP BY status",
        (kind,),
    ).fetchall()
    found = {r["status"]: int(r["n"]) for r in rows}
    return {"pending": found.get("pending", 0), "running": found.get("running", 0)}


def sync_and_answer(
    conn: Conn,
    config: SyncConfig,
    provider: ClientSource,
    *,
    now: datetime,
    handled_by: str,
    should_stop: Callable[[], bool] = never,
) -> tuple[SyncOutcome, list[int]]:
    """Claim the pending ``sync_now`` requests, run one pass and answer them.

    Used for every pass of the poller (scheduled or requested), so requests that arrive
    while a pass is due are answered by that pass. Returns the outcome and the request ids.
    """
    ids = claim_pending(conn, handled_by)
    try:
        outcome = sync_once(conn, config, provider, now=now, should_stop=should_stop)
    except BaseException:
        if ids:
            # If the database is gone, fail_orphans cleans up when the poller starts again.
            with contextlib.suppress(Exception):
                finish(conn, ids, error="interrupted: the sync pass did not complete")
        raise
    if ids:
        if outcome.ok and outcome.result is not None and not outcome.result.stopped_early:
            finish(conn, ids, result=outcome.result.request_result())
        elif outcome.ok:
            finish(conn, ids, error="interrupted: the sync service is stopping")
        else:
            finish(conn, ids, error=outcome.error or "sync failed")
    return outcome, ids
