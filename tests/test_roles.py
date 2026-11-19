"""coach_agent: exactly the rights of the roles table in docs/ARCHITECTURE.md.

SELECT on everything; INSERT on outbox and agent_requests (on agent_requests only the request
columns kind and payload); INSERT, UPDATE, DELETE on planned_workouts, races, segments and
segment_efforts; nothing else.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests import synthetic
from thresy import store

pytestmark = pytest.mark.db

AGENT_WRITES = {
    "outbox": {"INSERT"},
    "agent_requests": {"INSERT"},
    "planned_workouts": {"INSERT", "UPDATE", "DELETE"},
    "races": {"INSERT", "UPDATE", "DELETE"},
    "segments": {"INSERT", "UPDATE", "DELETE"},
    "segment_efforts": {"INSERT", "UPDATE", "DELETE"},
}
# Tables where the agent's INSERT is limited to some columns (all others take their defaults).
AGENT_INSERT_COLUMNS = {"agent_requests": {"kind", "payload"}}
AGENT_SEQUENCES = {
    "outbox_id_seq",
    "agent_requests_id_seq",
    "planned_workouts_id_seq",
    "races_id_seq",
}
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
# Privileges that can also be granted per column. They are checked with
# has_any_column_privilege, so a column-level grant on any table shows up in the matrix too.
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
# Tables the coordinator named explicitly; the parametrised tests below cover all others too.
PROTECTED = ("activities", "wellness_daily", "messages", "events", "sync_state")

DENIED = psycopg.errors.InsufficientPrivilege


def _relations(conn: psycopg.Connection[Any], kinds: tuple[str, ...]) -> list[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind = ANY(%s) ORDER BY 1",
        (list(kinds),),
    ).fetchall()
    return [r["relname"] for r in rows]


def _first_column(conn: psycopg.Connection[Any], table: str) -> str:
    return conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position LIMIT 1",
        (table,),
    ).fetchone()["column_name"]


# The privilege catalog ------------------------------------------------------------------------


def test_agent_table_privileges_are_exactly_the_contract(conn) -> None:
    relations = _relations(conn, ("r", "v"))
    assert set(AGENT_WRITES) <= set(relations) and "runs" in relations
    actual, expected = {}, {}
    for rel in relations:
        actual[rel] = {
            p
            for p in TABLE_PRIVILEGES
            if conn.execute(
                "SELECT has_any_column_privilege('coach_agent', %(rel)s, %(p)s) AS ok"
                if p in COLUMN_PRIVILEGES
                else "SELECT has_table_privilege('coach_agent', %(rel)s, %(p)s) AS ok",
                {"rel": f"public.{rel}", "p": p},
            ).fetchone()["ok"]
        }
        expected[rel] = {"SELECT"} | AGENT_WRITES.get(rel, set())
    assert actual == expected


def test_agent_column_restricted_inserts(conn) -> None:
    for table, allowed in AGENT_INSERT_COLUMNS.items():
        table_level = conn.execute(
            "SELECT has_table_privilege('coach_agent', %s, 'INSERT') AS ok", (f"public.{table}",)
        ).fetchone()["ok"]
        assert table_level is False, table
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        ).fetchall()
        insertable = {
            c["column_name"]
            for c in columns
            if conn.execute(
                "SELECT has_column_privilege('coach_agent', %s, %s, 'INSERT') AS ok",
                (f"public.{table}", c["column_name"]),
            ).fetchone()["ok"]
        }
        assert insertable == allowed, table


def test_agent_sequence_privileges(conn) -> None:
    sequences = _relations(conn, ("S",))
    assert set(sequences) >= AGENT_SEQUENCES
    for seq in sequences:
        privs = {
            p
            for p in ("USAGE", "SELECT", "UPDATE")
            if conn.execute(
                "SELECT has_sequence_privilege('coach_agent', %s, %s) AS ok", (f"public.{seq}", p)
            ).fetchone()["ok"]
        }
        assert privs == ({"USAGE"} if seq in AGENT_SEQUENCES else set()), seq


def test_agent_cannot_create_in_schema_or_database(conn) -> None:
    row = conn.execute(
        "SELECT has_schema_privilege('coach_agent', 'public', 'CREATE') AS schema_create,"
        " has_database_privilege('coach_agent', current_database(), 'CREATE') AS db_create,"
        " has_database_privilege('coach_agent', current_database(), 'TEMP') AS db_temp"
    ).fetchone()
    assert row == {"schema_create": False, "db_create": False, "db_temp": False}


# What the agent can do ------------------------------------------------------------------------


def test_agent_can_read_everything(conn, agent_conn) -> None:
    store.upsert_activity(conn, synthetic.activity(1001), source="test")
    for rel in _relations(conn, ("r", "v")):
        agent_conn.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.Identifier(rel)))
    assert agent_conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 1


def test_agent_can_queue_outbox_messages_but_not_change_them(conn, agent_conn) -> None:
    row = agent_conn.execute(
        "INSERT INTO outbox (kind, text) VALUES ('text', 'hello') RETURNING id"
    ).fetchone()
    assert row["id"] == 1
    with pytest.raises(DENIED):
        agent_conn.execute("UPDATE outbox SET sent_at = now()")
    with pytest.raises(DENIED):
        agent_conn.execute("DELETE FROM outbox")
    assert conn.execute("SELECT text, sent_at FROM outbox").fetchone() == {
        "text": "hello",
        "sent_at": None,
    }


def test_agent_can_create_requests_but_not_change_them(conn, agent_conn) -> None:
    row = agent_conn.execute(
        "INSERT INTO agent_requests (kind, payload) VALUES ('sync_now', '{}') RETURNING id"
    ).fetchone()
    assert row["id"] == 1
    # The agent reads its request back (that is how `coach sync request --wait` polls).
    mine = agent_conn.execute("SELECT status, result FROM agent_requests WHERE id = 1").fetchone()
    assert mine == {"status": "pending", "result": None}
    with pytest.raises(DENIED):
        agent_conn.execute("UPDATE agent_requests SET status = 'done'")
    with pytest.raises(DENIED):
        agent_conn.execute("DELETE FROM agent_requests")
    # Only kind and payload: no request that already looks finished, no chosen ids.
    for statement in (
        "INSERT INTO agent_requests (kind, status) VALUES ('sync_now', 'done')",
        "INSERT INTO agent_requests (kind, result) VALUES ('sync_now', '{}')",
        "INSERT INTO agent_requests (kind, handled_by) VALUES ('sync_now', 'me')",
        "INSERT INTO agent_requests (id, kind) VALUES (99, 'sync_now')",
    ):
        with pytest.raises(DENIED):
            agent_conn.execute(statement)
    assert conn.execute("SELECT status FROM agent_requests").fetchall() == [{"status": "pending"}]


def test_agent_can_manage_plans_races_and_segments(conn, agent_conn) -> None:
    store.upsert_activity(conn, synthetic.activity(1001), source="test")
    a = agent_conn
    wid = a.execute(
        "INSERT INTO planned_workouts (date, title) VALUES ('2030-01-03', 'Easy') RETURNING id"
    ).fetchone()["id"]
    a.execute("UPDATE planned_workouts SET status = 'done' WHERE id = %s", (wid,))
    rid = a.execute(
        "INSERT INTO races (date, name) VALUES ('2030-05-01', 'Spring 10k') RETURNING id"
    ).fetchone()["id"]
    a.execute("UPDATE races SET goal = 'finish' WHERE id = %s", (rid,))
    a.execute(
        "INSERT INTO segments (key, name, start_lat, start_lon, end_lat, end_lon)"
        " VALUES ('hill', 'Hill', 1, 2, 3, 4)"
    )
    a.execute("UPDATE segments SET notes = 'steep' WHERE key = 'hill'")
    a.execute(
        "INSERT INTO segment_efforts (segment_key, activity_id, start_utc, duration_s)"
        " VALUES ('hill', 1001, '2030-01-02 06:10:00+00', 300)"
    )
    a.execute("UPDATE segment_efforts SET duration_s = 290")
    assert conn.execute("SELECT status FROM planned_workouts").fetchone()["status"] == "done"
    assert conn.execute("SELECT goal FROM races").fetchone()["goal"] == "finish"
    assert conn.execute("SELECT duration_s FROM segment_efforts").fetchone()["duration_s"] == 290
    for table in ("segment_efforts", "segments", "races", "planned_workouts"):
        a.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
    for table in ("segment_efforts", "segments", "races", "planned_workouts"):
        query = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
        assert conn.execute(query).fetchone()["n"] == 0


# What the agent cannot do ---------------------------------------------------------------------


def _read_only_tables(conn) -> list[str]:
    return [t for t in _relations(conn, ("r",)) if t not in AGENT_WRITES]


@pytest.mark.parametrize("table", PROTECTED)
def test_agent_cannot_write_protected_tables(conn, agent_conn, table) -> None:
    column = _first_column(conn, table)
    statements = [
        sql.SQL("INSERT INTO {} DEFAULT VALUES").format(sql.Identifier(table)),
        sql.SQL("UPDATE {t} SET {c} = {c}").format(
            t=sql.Identifier(table), c=sql.Identifier(column)
        ),
        sql.SQL("DELETE FROM {}").format(sql.Identifier(table)),
        sql.SQL("TRUNCATE {}").format(sql.Identifier(table)),
    ]
    for statement in statements:
        with pytest.raises(DENIED):
            agent_conn.execute(statement)


def test_agent_cannot_write_any_other_table(conn, agent_conn) -> None:
    tables = _read_only_tables(conn)
    assert set(PROTECTED) <= set(tables) and "schema_migrations" in tables
    for table in tables:
        for statement in (
            sql.SQL("INSERT INTO {} DEFAULT VALUES"),
            sql.SQL("DELETE FROM {}"),
            sql.SQL("TRUNCATE {}"),
        ):
            with pytest.raises(DENIED):
                agent_conn.execute(statement.format(sql.Identifier(table)))
    with pytest.raises(DENIED):
        store.upsert_activity(agent_conn, synthetic.activity(1), source="agent")
    with pytest.raises(DENIED):
        store.set_sync_state(agent_conn, "k", 1)


@pytest.mark.parametrize("table", sorted(AGENT_WRITES))
def test_agent_cannot_truncate_even_writable_tables(agent_conn, table) -> None:
    with pytest.raises(DENIED):
        agent_conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE intruder (id int)",
        "CREATE TEMP TABLE scratch (id int)",
        "CREATE VIEW v AS SELECT 1",
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE SCHEMA mine",
        "CREATE INDEX ON planned_workouts (title)",
        "ALTER TABLE planned_workouts ADD COLUMN x int",
        "DROP TABLE segment_efforts",
        "DROP VIEW runs",
        "SELECT setval('outbox_id_seq', 1000)",
    ],
)
def test_agent_cannot_create_alter_or_drop(agent_conn, statement) -> None:
    with pytest.raises(DENIED):
        agent_conn.execute(statement)
