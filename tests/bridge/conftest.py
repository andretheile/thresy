"""A bridge wired to the test database, a FakeRunner, a FakeTransport and a FakeClock.

Tests are plain functions that run one coroutine with ``asyncio.run``::

    def test_x(make_harness):
        async def scenario():
            async with make_harness() as h:
                await h.say("Hi")
                await h.settle()
                assert h.transport.texts == [...]
        asyncio.run(scenario())
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from thresy import db
from thresy.bridge.app import Bridge
from thresy.bridge.clock import FakeClock
from thresy.bridge.repo import Repo
from thresy.bridge.settings import BridgeSettings
from thresy.config import Settings
from thresy.runner import FakeRunner, RunResult
from thresy.runner.handle import RunContext
from thresy.transport import FakeTransport, InboundMessage

pytestmark = pytest.mark.db

TZ = "America/Chicago"
ZONE = ZoneInfo(TZ)
CHAT = "100"
TOKEN = "123456:TEST-TOKEN-NOT-REAL"
# Wednesday 2030-01-02, 08:00 local (07:00 UTC)
START = datetime(2030, 1, 2, 8, 0, tzinfo=ZONE)

AGENT_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_requests (
  id bigserial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending',
  result jsonb, error text, run_id bigint,
  started_at timestamptz, finished_at timestamptz, handled_by text
)
"""


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=ZONE)


class GatedRunner(FakeRunner):
    """A FakeRunner whose runs wait for ``gate`` (or a cancel) before they proceed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def _drive(self, req, ctx: RunContext) -> RunResult:
        self.started.set()
        while not self.gate.is_set() and not ctx.cancel_event.is_set():
            await asyncio.sleep(0.005)
        return await super()._drive(req, ctx)


class OtherRunner(FakeRunner):
    name = "other-fake"


@dataclass
class Harness:
    bridge: Bridge
    repo: Repo
    runner: FakeRunner
    transport: FakeTransport
    clock: FakeClock
    conn: psycopg.Connection[Any]
    settings: Settings
    bridge_settings: BridgeSettings
    workspace: Path

    async def say(self, text: str, *, chat: str = CHAT, **kw: Any) -> None:
        kw.setdefault("kind", "text")
        kw.setdefault("ts", self.clock.now())
        await self.transport.receive(
            InboundMessage(transport=self.transport.name, chat_id=chat, text=text, **kw)
        )

    async def settle(self) -> None:
        await asyncio.wait_for(self.bridge.settle(), timeout=10)

    async def tick(self) -> None:
        await self.bridge.events.tick()
        await self.settle()

    def rows(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        return self.conn.execute(query, params).fetchall()

    def one(self, query: str, params: Any = None) -> dict[str, Any]:
        row = self.conn.execute(query, params).fetchone()
        assert row is not None
        return row


def make_settings(test_db, tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": test_db.app_url,
        "database_url_ro": test_db.ro_url,
        "agent_database_url": test_db.agent_url,
        "workspace_dir": tmp_path / "workspace",
        "data_dir": tmp_path / "data",
        "tz": TZ,
        "language": "en",
        "name": "Thresy",
        "runner": "fake",
        "telegram_token": TOKEN,
        "telegram_chat_ids": CHAT,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_bridge_settings(**overrides: Any) -> BridgeSettings:
    values: dict[str, Any] = {
        "bridge_quiet_hours": "off",
        "bridge_run_timeout_s": 30,
        "bridge_briefing_settle_s": 0,  # tests of the settle delay set it themselves
        "home_location": None,  # not from the developer's environment
    }
    values.update(overrides)
    return BridgeSettings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def no_route_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """None of the developer's ``COACH_ROUTE_*`` values: the bridge hands the agent those it
    finds in its environment and ``.env`` (``thresy.tools.route.RouteSettings``). Here only the
    environment counts, so a test sets a value with ``monkeypatch.setenv``."""
    from pydantic_settings import SettingsConfigDict

    from thresy.tools import route

    for name in [n for n in os.environ if n.startswith("COACH_ROUTE_")]:
        monkeypatch.delenv(name)

    class RouteSettingsWithoutDotenv(route.RouteSettings):
        model_config = SettingsConfigDict(**{**route.RouteSettings.model_config, "env_file": None})

    monkeypatch.setattr(route, "RouteSettings", RouteSettingsWithoutDotenv)


@pytest.fixture
def bridge_db(conn, test_db) -> Iterator[Any]:
    """The emptied test database, with ``agent_requests`` until its migration exists."""
    exists = conn.execute("SELECT to_regclass('public.agent_requests') AS t").fetchone()["t"]
    if exists is None:
        conn.execute(AGENT_REQUESTS_DDL)
    try:
        yield test_db
    finally:
        if exists is None:
            conn.execute("DROP TABLE IF EXISTS agent_requests")


@pytest.fixture
def make_harness(bridge_db, conn, tmp_path) -> Callable[..., Any]:
    @contextlib.asynccontextmanager
    async def factory(
        runner: FakeRunner | None = None,
        *,
        transport: FakeTransport | None = None,
        clock: FakeClock | None = None,
        start: bool = True,
        settings: dict[str, Any] | None = None,
        bridge: dict[str, Any] | None = None,
    ) -> AsyncIterator[Harness]:
        core = make_settings(bridge_db, tmp_path, **(settings or {}))
        bs = make_bridge_settings(**(bridge or {}))
        clock = clock if clock is not None else FakeClock(START)
        repo = Repo(bridge_db.app_url, clock=clock)
        await repo.open()
        runner = runner if runner is not None else FakeRunner(reply="OK")
        transport = transport if transport is not None else FakeTransport(chat_ids=(CHAT,))
        b = Bridge(
            settings=core,
            bridge_settings=bs,
            repo=repo,
            runner=runner,
            transport=transport,
            clock=clock,
        )
        b.commands.sync_poll_s = 0.01
        harness = Harness(
            bridge=b,
            repo=repo,
            runner=runner,
            transport=transport,
            clock=clock,
            conn=conn,
            settings=core,
            bridge_settings=bs,
            workspace=b.workspace,
        )
        try:
            if start:
                await b.start(background=False)
            yield harness
        finally:
            await b.shutdown()
            await repo.close()

    return factory


@pytest.fixture
def agent_db_url(test_db) -> str:
    return test_db.agent_url


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def connect_app(test_db) -> psycopg.Connection[Any]:
    return db.connect(test_db.app_url)
