"""The bridge's SQL, on an async connection pool (role ``coach_app``).

Tables used: ``messages``, ``agent_sessions``, ``agent_runs``, ``outbox``, ``events``,
``agent_requests`` and ``sync_state`` (keys prefixed ``bridge:`` for the bridge's own small
state: the day of the last morning briefing and when each notification kind was last sent).
Reads of Garmin data (``wellness_daily``, ``activities``, ``runs``) serve the status command
and the numbers-only briefing.

Every timestamp the bridge writes comes from its clock (not the database's ``now()``), so the
injected clock in tests and the times in the tables agree.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from thresy.store import to_jsonb

from .clock import Clock, SystemClock

Row = dict[str, Any]
STATE_PREFIX = "bridge:"


class Repo:
    def __init__(self, url: str, *, clock: Clock | None = None, max_size: int = 4) -> None:
        self.clock: Clock = clock or SystemClock()
        self.pool = AsyncConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            open=False,
            name="bridge",
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "options": "-c TimeZone=UTC",
                "connect_timeout": 10,
            },
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self.pool.close()

    # -- helpers ---------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    async def _all(self, query: str, params: Sequence[Any] | None = None) -> list[Row]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            return await cur.fetchall()

    async def _one(self, query: str, params: Sequence[Any] | None = None) -> Row | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            return await cur.fetchone()

    async def _exec(self, query: str, params: Sequence[Any] | None = None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            return cur.rowcount

    # -- messages --------------------------------------------------------------------------------

    async def log_message(
        self,
        *,
        chat_id: str,
        direction: str,
        kind: str,
        text: str | None,
        transport: str,
        attachments: Any = None,
        transport_msg_id: str | None = None,
        run_id: int | None = None,
        ts: datetime | None = None,
    ) -> int:
        row = await self._one(
            "INSERT INTO messages (ts, chat_id, direction, kind, text, attachments, transport,"
            " transport_msg_id, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                ts or self._now(),
                chat_id,
                direction,
                kind,
                text,
                to_jsonb(attachments) if attachments is not None else None,
                transport,
                transport_msg_id,
                run_id,
            ),
        )
        assert row is not None
        return int(row["id"])

    async def attach_messages(self, ids: Iterable[int], run_id: int) -> None:
        ids = list(ids)
        if ids:
            await self._exec("UPDATE messages SET run_id = %s WHERE id = ANY(%s)", (run_id, ids))

    async def recent_messages(
        self,
        chat_id: str,
        since: datetime,
        exclude: Sequence[int],
        limit: int,
        after_id: int = 0,
    ) -> list[Row]:
        """The conversation for a context block: the athlete's messages and the agent's
        (outgoing rows without a run are the bridge's own replies and notices; skipped)."""
        rows = await self._all(
            "SELECT id, ts, direction, kind, text, attachments FROM messages"
            " WHERE chat_id = %s AND ts >= %s AND id > %s AND NOT (id = ANY(%s))"
            " AND kind <> 'command' AND (direction = 'in' OR run_id IS NOT NULL)"
            " ORDER BY ts DESC, id DESC LIMIT %s",
            (chat_id, since, after_id, list(exclude), limit),
        )
        rows.reverse()
        return rows

    async def last_message_id(self, chat_id: str) -> int:
        row = await self._one(
            "SELECT coalesce(max(id), 0) AS id FROM messages WHERE chat_id = %s", (chat_id,)
        )
        return int(row["id"]) if row else 0

    async def unanswered_inbound(self, since: datetime) -> list[Row]:
        return await self._all(
            "SELECT * FROM messages WHERE direction = 'in' AND run_id IS NULL"
            " AND kind <> 'command' AND ts >= %s ORDER BY id",
            (since,),
        )

    async def choice_answered(self, chat_id: str, ref: str) -> bool:
        row = await self._one(
            "SELECT 1 AS x FROM messages WHERE chat_id = %s AND direction = 'in'"
            " AND kind = 'choice_answer' AND attachments->>'reply_to' = %s LIMIT 1",
            (chat_id, ref),
        )
        return row is not None

    # -- sessions --------------------------------------------------------------------------------

    async def active_session(self, chat_id: str) -> Row | None:
        return await self._one(
            "SELECT * FROM agent_sessions WHERE chat_id = %s AND status = 'active'"
            " ORDER BY started_at DESC LIMIT 1",
            (chat_id,),
        )

    async def create_session(self, chat_id: str, runner: str, day: date) -> Row:
        """A new active session; any other active session of the chat is closed."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_sessions SET status = 'closed'"
                " WHERE chat_id = %s AND status = 'active'",
                (chat_id,),
            )
            cur = await conn.execute(
                "INSERT INTO agent_sessions (id, chat_id, runner, day, status, started_at)"
                " VALUES (%s, %s, %s, %s, 'active', %s) RETURNING *",
                (uuid.uuid4(), chat_id, runner, day, self._now()),
            )
            row = await cur.fetchone()
        assert row is not None
        return row

    async def set_session_status(self, session_id: Any, status: str) -> None:
        await self._exec(
            "UPDATE agent_sessions SET status = %s WHERE id = %s", (status, session_id)
        )

    async def set_native_session(self, session_id: Any, native: str) -> None:
        await self._exec(
            "UPDATE agent_sessions SET native_session_id = %s, last_used_at = %s WHERE id = %s",
            (native, self._now(), session_id),
        )

    async def touch_session(self, session_id: Any) -> None:
        await self._exec(
            "UPDATE agent_sessions SET last_used_at = %s WHERE id = %s", (self._now(), session_id)
        )

    # -- runs ------------------------------------------------------------------------------------

    async def create_run(self, session_id: Any, trigger: str, runner: str) -> int:
        row = await self._one(
            "INSERT INTO agent_runs (session_id, trigger, runner, status, started_at)"
            " VALUES (%s, %s, %s, 'running', %s) RETURNING id",
            (session_id, trigger, runner, self._now()),
        )
        assert row is not None
        return int(row["id"])

    async def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        model: str | None = None,
        cost_usd: float | None = None,
        usage: dict | None = None,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        await self._exec(
            "UPDATE agent_runs SET status = %s, finished_at = %s, model = coalesce(%s, model),"
            " cost_usd = %s, usage = %s, error_kind = %s, error_detail = %s WHERE id = %s",
            (
                status,
                self._now(),
                model,
                cost_usd,
                Jsonb(usage) if usage is not None else None,
                error_kind,
                (error_detail or "")[:4000] or None,
                run_id,
            ),
        )

    async def fail_running_runs(self, detail: str) -> list[Row]:
        """Mark runs left 'running' by a previous process as failed; return them with chats."""
        return await self._all(
            "UPDATE agent_runs r SET status = 'failed', finished_at = %s,"
            " error_kind = 'interrupted', error_detail = %s"
            " FROM agent_sessions s WHERE r.status = 'running' AND s.id = r.session_id"
            " RETURNING r.id, r.trigger, s.chat_id",
            (self._now(), detail),
        )

    async def run_stats(self, since: datetime) -> Row:
        row = await self._one(
            "SELECT count(*) AS count, sum(cost_usd) AS cost FROM agent_runs"
            " WHERE started_at >= %s",
            (since,),
        )
        assert row is not None
        return row

    async def outbox_rows_for_run(self, run_id: int) -> int:
        row = await self._one("SELECT count(*) AS n FROM outbox WHERE run_id = %s", (run_id,))
        return int(row["n"]) if row else 0

    # -- outbox ----------------------------------------------------------------------------------

    async def queue(
        self,
        *,
        chat_id: str | None,
        kind: str,
        text: str | None = None,
        run_id: int | None = None,
        file_path: str | None = None,
        options: list[str] | None = None,
    ) -> int:
        row = await self._one(
            "INSERT INTO outbox (created_at, chat_id, kind, text, run_id, file_path, options)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                self._now(),
                chat_id,
                kind,
                text,
                run_id,
                file_path,
                Jsonb(options) if options else None,
            ),
        )
        assert row is not None
        return int(row["id"])

    async def pending_outbox(self, limit: int = 50) -> list[Row]:
        return await self._all(
            "SELECT o.*, s.chat_id AS run_chat_id FROM outbox o"
            " LEFT JOIN agent_runs r ON r.id = o.run_id"
            " LEFT JOIN agent_sessions s ON s.id = r.session_id"
            " WHERE o.sent_at IS NULL AND o.error IS NULL ORDER BY o.id LIMIT %s",
            (limit,),
        )

    async def outbox_row(self, outbox_id: int) -> Row | None:
        return await self._one("SELECT * FROM outbox WHERE id = %s", (outbox_id,))

    async def mark_sent(
        self,
        row: Row,
        *,
        chat_id: str,
        transport: str,
        transport_msg_id: str,
        attachments: Any,
    ) -> None:
        """Mark an outbox row sent and log it as an outgoing message, atomically."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE outbox SET sent_at = %s, transport_msg_id = %s, error = NULL,"
                " chat_id = coalesce(chat_id, %s) WHERE id = %s",
                (self._now(), transport_msg_id, chat_id, row["id"]),
            )
            await conn.execute(
                "INSERT INTO messages (ts, chat_id, direction, kind, text, attachments, transport,"
                " transport_msg_id, run_id) VALUES (%s, %s, 'out', %s, %s, %s, %s, %s, %s)",
                (
                    self._now(),
                    chat_id,
                    row["kind"],
                    row["text"],
                    to_jsonb(attachments) if attachments else None,
                    transport,
                    transport_msg_id,
                    await self._existing_run(conn, row.get("run_id")),
                ),
            )

    @staticmethod
    async def _existing_run(conn: Any, run_id: Any) -> int | None:
        # outbox.run_id has no foreign key (the agent writes it); messages.run_id has one.
        if run_id is None:
            return None
        cur = await conn.execute("SELECT 1 AS x FROM agent_runs WHERE id = %s", (run_id,))
        return int(run_id) if await cur.fetchone() else None

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        await self._exec(
            "UPDATE outbox SET error = %s WHERE id = %s", ((error or "failed")[:2000], outbox_id)
        )

    async def outbox_stats(self, since: datetime) -> Row:
        row = await self._one(
            "SELECT count(*) FILTER (WHERE sent_at IS NULL AND error IS NULL) AS pending,"
            " count(*) FILTER (WHERE error IS NOT NULL AND created_at >= %s) AS failed"
            " FROM outbox",
            (since,),
        )
        assert row is not None
        return row

    # -- events ----------------------------------------------------------------------------------

    async def unprocessed_events(self, limit: int = 200) -> list[Row]:
        return await self._all(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id LIMIT %s", (limit,)
        )

    async def mark_events(self, ids: Iterable[int], processed_by: str) -> None:
        ids = list(ids)
        if ids:
            await self._exec(
                "UPDATE events SET processed_at = %s, processed_by = %s"
                " WHERE id = ANY(%s) AND processed_at IS NULL",
                (self._now(), processed_by, ids),
            )

    async def count_events(self, kind: str, since: datetime) -> Row:
        row = await self._one(
            "SELECT count(*) AS n, min(ts) AS first FROM events WHERE kind = %s AND ts >= %s",
            (kind, since),
        )
        assert row is not None
        return row

    async def latest_unprocessed_problem(self) -> Row | None:
        return await self._one(
            "SELECT kind, ts FROM events WHERE kind IN ('garmin_auth_required', 'sync_failed')"
            " ORDER BY id DESC LIMIT 1"
        )

    # -- state -----------------------------------------------------------------------------------

    async def get_state(self, key: str) -> Any:
        row = await self._one("SELECT value FROM sync_state WHERE key = %s", (STATE_PREFIX + key,))
        return None if row is None else row["value"]

    async def set_state(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (STATE_PREFIX + key, to_jsonb(value), self._now()),
        )

    async def sync_status(self) -> Row:
        """What /status can say about the sync service without knowing its internals."""
        row = await self._one(
            "SELECT"
            " (SELECT value FROM sync_state WHERE key = 'last_success') AS last_success,"
            " (SELECT max(updated_at) FROM sync_state WHERE key NOT LIKE 'bridge:%')"
            "   AS last_state_change,"
            " (SELECT max(local_date) FROM activities) AS newest_activity,"
            " (SELECT max(wake_date) FROM wellness_daily WHERE sleep_synced_at IS NOT NULL)"
            "   AS newest_sleep"
        )
        assert row is not None
        return row

    # -- agent requests --------------------------------------------------------------------------

    async def insert_request(self, kind: str, payload: dict[str, Any]) -> int:
        row = await self._one(
            "INSERT INTO agent_requests (kind, payload, created_at) VALUES (%s, %s, %s)"
            " RETURNING id",
            (kind, Jsonb(payload), self._now()),
        )
        assert row is not None
        return int(row["id"])

    async def request(self, request_id: int) -> Row | None:
        return await self._one("SELECT * FROM agent_requests WHERE id = %s", (request_id,))

    # -- Garmin data -----------------------------------------------------------------------------

    async def wellness(self, wake_date: date) -> Row | None:
        return await self._one("SELECT * FROM wellness_daily WHERE wake_date = %s", (wake_date,))

    async def sleep_synced(self, wake_date: date) -> bool:
        row = await self._one(
            "SELECT 1 AS x FROM wellness_daily WHERE wake_date = %s"
            " AND sleep_synced_at IS NOT NULL",
            (wake_date,),
        )
        return row is not None

    async def runs_on(self, local_date: date) -> list[Row]:
        return await self._all(
            "SELECT id, start_local, distance_km, coalesce(nullif(moving_s, 0), duration_s)"
            " AS time_s, pace, avg_hr FROM runs WHERE local_date = %s ORDER BY start_utc",
            (local_date,),
        )

    async def activity(self, activity_id: int) -> Row | None:
        return await self._one(
            "SELECT id, type, is_run, start_utc, start_local, local_date, distance_m"
            " FROM activities WHERE id = %s",
            (activity_id,),
        )
