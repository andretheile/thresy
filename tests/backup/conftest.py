"""Fixtures for the backup tests: stand-ins for age, pg_dump and the GCS client, the real
PostgreSQL tools when they are installed, and temporary CREATEDB for coach_app."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from thresy import db
from thresy.backup import pg

_FAKE_AGE = """\
#!{python}
# Stand-in for age: "encrypts" by reversing the bytes behind a header naming the recipients.
import sys

args = sys.argv[1:]
recipients, identity, output, decrypt = [], None, None, False
source = None
while args:
    arg = args.pop(0)
    if arg == "--recipient":
        recipients.append(args.pop(0))
    elif arg == "--identity":
        identity = args.pop(0)
    elif arg == "--output":
        output = args.pop(0)
    elif arg == "--decrypt":
        decrypt = True
    else:
        source = arg
data = open(source, "rb").read()
if decrypt:
    key = open(identity).read().strip()
    header, _, body = data.partition(b"\\n")
    if not header.startswith(b"FAKE-AGE ") or key.encode() not in header.split(b" ")[1].split(b","):
        sys.stderr.write("no identity matched any of the recipients\\n")
        sys.exit(1)
    open(output, "wb").write(body[::-1])
else:
    open(output, "wb").write(b"FAKE-AGE " + ",".join(recipients).encode() + b"\\n" + data[::-1])
"""

_FAKE_PG_DUMP = """\
#!{python}
# Stand-in for pg_dump: writes a small file, records how it was called.
import json, os, sys

if sys.argv[1:] == ["--version"]:
    print("pg_dump (PostgreSQL) {major}.99")
    sys.exit(0)
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
if {fail!r}:
    sys.stderr.write("pg_dump: error: connection to server failed\\n")
    sys.exit(1)
target = [a for a in sys.argv if a.startswith("--file=")][0][len("--file="):]
snapshot = [a for a in sys.argv if a.startswith("--snapshot=")]
with open(target, "wb") as fh:
    fh.write(b"PGDMP fake dump " + (snapshot[0].encode() if snapshot else b""))
"""


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_age(tmp_path: Path) -> Path:
    directory = tmp_path / "age-bin"
    directory.mkdir()
    return _script(directory / "age", _FAKE_AGE.format(python=sys.executable))


@pytest.fixture
def server_major(test_db) -> int:
    with db.connect(test_db.app_url) as conn:
        return pg.server_major(conn)


@dataclass
class FakePg:
    bin_dir: Path
    log: Path

    def calls(self) -> list[dict[str, Any]]:
        import json

        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def _fake_pg(tmp_path: Path, major: int, *, fail: bool) -> FakePg:
    directory = tmp_path / ("pg-bin-failing" if fail else "pg-bin")
    directory.mkdir()
    log = directory / "calls.jsonl"
    _script(
        directory / "pg_dump",
        _FAKE_PG_DUMP.format(python=sys.executable, major=major, log=str(log), fail=fail),
    )
    return FakePg(directory, log)


@pytest.fixture
def fake_pg(tmp_path: Path, server_major: int) -> FakePg:
    return _fake_pg(tmp_path, server_major, fail=False)


@pytest.fixture
def failing_pg(tmp_path: Path, server_major: int) -> FakePg:
    return _fake_pg(tmp_path, server_major, fail=True)


@pytest.fixture
def real_pg_bin(server_major: int) -> Path | None:
    """The real pg_dump/pg_restore for the server's major version, or skip."""
    configured = os.environ.get("COACH_BACKUP_PG_BIN_DIR")
    bin_dir = Path(configured) if configured else None
    try:
        dump = pg.find_tool("pg_dump", server_major, bin_dir)
        pg.find_tool("pg_restore", server_major, bin_dir)
    except pg.PgError as exc:
        pytest.skip(f"no PostgreSQL {server_major} client tools: {exc}")
    return Path(dump.path).parent


@pytest.fixture
def real_age() -> tuple[str, str]:
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.skip("age and age-keygen are not installed")
    return shutil.which("age") or "age", shutil.which("age-keygen") or "age-keygen"


@pytest.fixture
def createdb(pg_server, test_db) -> Iterator[None]:
    """Let coach_app create databases for the duration of a test (as the production role
    does, docker/postgres/initdb/10-roles.sh); restores the previous setting."""
    with pg_server.admin() as conn:
        before = conn.execute("SELECT rolcreatedb FROM pg_roles WHERE rolname = 'coach_app'")
        had = bool(before.fetchone()[0])
        if not had:
            conn.execute("ALTER ROLE coach_app CREATEDB")
    try:
        yield
    finally:
        if not had:
            with pg_server.admin() as conn:
                conn.execute("ALTER ROLE coach_app NOCREATEDB")


# -- a stand-in for google.cloud.storage.Client ---------------------------------------------------


@dataclass
class FakeGcs:
    """Records every call; only bucket(), blob() and upload_from_filename() exist, so any
    attempt to list, read, overwrite or delete fails loudly."""

    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    refuse: str | None = None  # "forbidden" | "notfound"

    def bucket(self, name: str) -> FakeBucket:
        self.calls.append(("bucket", name))
        return FakeBucket(self, name)


@dataclass
class FakeBucket:
    client: FakeGcs
    name: str

    def blob(self, name: str) -> FakeBlob:
        self.client.calls.append(("blob", name))
        return FakeBlob(self.client, self.name, name)


@dataclass
class FakeBlob:
    client: FakeGcs
    bucket: str
    name: str

    def upload_from_filename(self, filename: str, **kwargs: Any) -> None:
        from google.api_core import exceptions

        self.client.calls.append(("upload", self.name, kwargs))
        assert kwargs.get("if_generation_match") == 0, "uploads must be create-only"
        if self.client.refuse == "forbidden":
            raise exceptions.Forbidden("caller does not have storage.objects.create access")
        if self.client.refuse == "notfound":
            raise exceptions.NotFound("bucket does not exist")
        key = (self.bucket, self.name)
        if key in self.client.objects:
            raise exceptions.PreconditionFailed("At least one of the pre-conditions failed")
        self.client.objects[key] = Path(filename).read_bytes()


@pytest.fixture
def gcs() -> FakeGcs:
    return FakeGcs()
