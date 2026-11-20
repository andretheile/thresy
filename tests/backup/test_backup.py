"""Backup runs and restore checks against the test database.

Most tests use a stand-in pg_dump (the development Mac has no PostgreSQL client); the
``real_pg_bin`` tests use the real pg_dump and pg_restore of the server's major version and
are skipped without them (docker/backup-test.sh runs them in a container that has them).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests import synthetic
from thresy import store
from thresy.backup import BackupConfig, BackupError, core, open_target, restore_check, run_backup
from thresy.backup.core import BackupInterrupted

pytestmark = pytest.mark.db

D = date(2030, 1, 2)


@pytest.fixture
def data(conn, tmp_path: Path) -> dict[str, Path]:
    """Synthetic rows, two FIT files and a workspace with memory files and kept scripts."""
    store.upsert_activity(conn, synthetic.activity(1001), source="test")
    store.upsert_activity(conn, synthetic.activity(1002), source="test")
    store.upsert_wellness(conn, D, sleep=synthetic.sleep(D), hrv=synthetic.hrv(D))
    store.set_sync_state(conn, "sync_status", {"last_success_at": "2030-01-02T06:00:00+00:00"})
    data_dir = tmp_path / "data"
    for activity_id in (1001, 1002):
        path = data_dir / "fit" / "2030" / f"{activity_id}.fit"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b".FIT" + bytes([activity_id % 256]) * 64)
    workspace = tmp_path / "workspace"
    (workspace / "memory" / "notes").mkdir(parents=True)
    (workspace / "memory" / "profile.md").write_text("# Profile\nsynthetic athlete\n")
    (workspace / "memory" / "notes" / "2030-01.md").write_text("a note\n")
    (workspace / "memory" / "link.md").symlink_to(workspace / "memory" / "profile.md")
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "climb.py").write_text('"""A synthetic question?"""\n')
    (workspace / "scripts" / "_shared.py").write_text("X = 1\n")
    (workspace / "scripts" / "linked.py").symlink_to(workspace / "memory" / "profile.md")
    (workspace / "AGENTS.md").write_text("not backed up\n")
    return {"data": data_dir, "workspace": workspace, "target": tmp_path / "target"}


def make_config(test_db, data: dict[str, Path], **kw: Any) -> BackupConfig:
    kw.setdefault("target", open_target(data["target"].as_uri()))
    return BackupConfig(
        database_url=test_db.app_url,
        data_dir=data["data"],
        workspace_dir=data["workspace"],
        **kw,
    )


def files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run_dir(target: Path) -> Path:
    runs = sorted((target / "runs").iterdir())
    return runs[-1]


# -- runs with a stand-in pg_dump -----------------------------------------------------------------


def test_backup_to_a_local_directory(conn, test_db, data, fake_pg):
    config = make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir)
    result = run_backup(config)
    assert result.ok
    assert result.fit_uploaded == 2 and result.fit_total == 2 and result.memory_files == 2
    stored = files(data["target"])
    prefix = f"runs/{result.run_id}/"
    assert set(stored) == {
        prefix + "thresy.dump",
        prefix + "memory.tar.gz",
        prefix + "scripts.tar.gz",
        prefix + "manifest.json",
        "fit/2030/1001.fit",
        "fit/2030/1002.fit",
    }
    assert stored[prefix + "thresy.dump"].startswith(b"PGDMP fake dump ")
    assert stored["fit/2030/1001.fit"] == (data["data"] / "fit/2030/1001.fit").read_bytes()
    with tarfile.open(fileobj=io.BytesIO(stored[prefix + "memory.tar.gz"])) as archive:
        names = sorted(archive.getnames())
    assert names == ["memory", "memory/notes", "memory/notes/2030-01.md", "memory/profile.md"]
    with tarfile.open(fileobj=io.BytesIO(stored[prefix + "scripts.tar.gz"])) as archive:
        names = sorted(archive.getnames())  # the link is skipped, never followed
    assert names == ["scripts", "scripts/_shared.py", "scripts/climb.py"]
    assert result.scripts_files == 2 and "kept scripts: 2" in result.text()

    manifest = json.loads(stored[prefix + "manifest.json"])
    assert manifest["run_id"] == result.run_id and manifest["encrypted"] is False
    counts = manifest["row_counts"]
    assert counts["activities"] == 2 and counts["wellness_daily"] == 1
    # The snapshot: sync_status, and backup_status with this attempt (written before the dump).
    assert counts["sync_state"] == 2
    assert {o["name"] for o in manifest["objects"]} == {
        prefix + "thresy.dump",
        prefix + "memory.tar.gz",
        prefix + "scripts.tar.gz",
    }
    assert (manifest["memory_files"], manifest["scripts_files"]) == (2, 2)
    assert manifest["fit"] == {"uploaded": 2, "already_there": 0, "total": 2, "prefix": "fit/"}

    # pg_dump got the dumped snapshot and no secret on its command line.
    call = fake_pg.calls()[0]
    assert any(a.startswith("--snapshot=") for a in call["argv"])
    assert "coach_app" not in " ".join(call["argv"])
    assert "PGPASSWORD" in call["env"] and "COACH_DATABASE_URL" not in call["env"]

    status = core.get_status(conn)
    assert status["last_run_id"] == result.run_id and status["failures_in_row"] == 0
    assert status["last_result"]["fit_uploaded"] == 2
    assert status["last_result"]["scripts_files"] == 2


def test_memory_and_scripts_restore_into_a_new_workspace(conn, test_db, data, fake_pg, tmp_path):
    """The restore steps of docs/backup.md: unpack both archives, import them with
    ``coach workspace init --memory-from`` / ``--scripts-from``."""
    from thresy.workspace import import_memory, import_scripts, init_workspace

    result = run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    run = data["target"] / "runs" / result.run_id
    unpacked = tmp_path / "restore"
    for name in ("memory.tar.gz", "scripts.tar.gz"):
        with tarfile.open(run / name) as archive:
            archive.extractall(unpacked, filter="data")
    fresh = tmp_path / "new-workspace"
    init_workspace(fresh)  # the repository's template
    import_memory(fresh, unpacked / "memory")
    import_scripts(fresh, unpacked / "scripts")
    for rel in ("memory/profile.md", "memory/notes/2030-01.md", "scripts/climb.py"):
        assert (fresh / rel).read_bytes() == (data["workspace"] / rel).read_bytes()


def test_fit_files_are_uploaded_once(conn, test_db, data, fake_pg):
    config = make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir)
    run_backup(config)
    again = run_backup(config)
    assert (again.fit_uploaded, again.fit_present, again.fit_total) == (0, 0, 2)
    new = data["data"] / "fit" / "2030" / "1003.fit"
    new.write_bytes(b".FIT new")
    third = run_backup(config)
    assert third.fit_uploaded == 1 and third.fit_total == 3
    # Lost state: everything is offered again; the target already has the files.
    conn.execute("DELETE FROM sync_state WHERE key = %s", (core.FIT_KEY,))
    fourth = run_backup(config)
    assert (fourth.fit_uploaded, fourth.fit_present) == (0, 3)
    assert len(list((data["target"] / "runs").iterdir())) == 4  # a directory per run


def test_encrypted_backup(conn, test_db, data, fake_pg, fake_age, tmp_path):
    config = make_config(
        test_db, data, pg_bin_dir=fake_pg.bin_dir, recipients=["age1test"], age_bin=str(fake_age)
    )
    result = run_backup(config)
    stored = files(data["target"])
    assert stored and all(name.endswith(".age") for name in stored)
    for blob in stored.values():
        assert b"synthetic athlete" not in blob and b"PGDMP" not in blob and b".FIT" not in blob
    # The private key restores them (here: the stand-in's).
    key = tmp_path / "key.txt"
    key.write_text("age1test\n")
    from thresy.backup import crypto

    out = tmp_path / "memory.tar.gz"
    crypto.decrypt(
        data["target"] / f"runs/{result.run_id}/memory.tar.gz.age", out, key, binary=str(fake_age)
    )
    with tarfile.open(out) as archive:
        assert "memory/profile.md" in archive.getnames()
    # Switching encryption on or off starts the FIT record over (new object names).
    plain = run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    assert plain.fit_uploaded == 2


def test_backup_to_gcs_creates_objects_only(conn, test_db, data, fake_pg, gcs):
    target = open_target("gs://example-bucket/thresy", client=gcs)
    config = make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir, target=target)
    result = run_backup(config)
    names = sorted(name for _, name in gcs.objects)
    assert names == sorted(
        [
            "thresy/fit/2030/1001.fit",
            "thresy/fit/2030/1002.fit",
            f"thresy/runs/{result.run_id}/manifest.json",
            f"thresy/runs/{result.run_id}/memory.tar.gz",
            f"thresy/runs/{result.run_id}/scripts.tar.gz",
            f"thresy/runs/{result.run_id}/thresy.dump",
        ]
    )
    assert {call[0] for call in gcs.calls} == {"bucket", "blob", "upload"}
    conn.execute("DELETE FROM sync_state WHERE key = %s", (core.FIT_KEY,))
    again = run_backup(config)
    assert (again.fit_uploaded, again.fit_present) == (0, 2)  # 412: already there


def test_failure_is_recorded_and_raises_one_event_per_day(conn, test_db, data, failing_pg):
    config = make_config(test_db, data, pg_bin_dir=failing_pg.bin_dir)
    with pytest.raises(BackupError, match="pg_dump failed"):
        run_backup(config)
    with pytest.raises(BackupError):
        run_backup(config)
    status = core.get_status(conn)
    assert status["failures_in_row"] == 2 and "connection to server failed" in status["last_error"]
    events = conn.execute("SELECT kind, payload, dedupe_key FROM events").fetchall()
    assert len(events) == 1
    assert events[0]["kind"] == "backup_failed"
    assert events[0]["dedupe_key"].startswith("backup_failed:")
    assert "pg_dump failed" in events[0]["payload"]["error"]
    assert files(data["target"]) == {}


def test_a_target_failure_is_a_backup_failure(conn, test_db, data, fake_pg, gcs):
    gcs.refuse = "forbidden"
    target = open_target("gs://example-bucket/thresy", client=gcs)
    config = make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir, target=target)
    with pytest.raises(BackupError, match="objectCreator"):
        run_backup(config)
    assert core.get_status(conn)["failures_in_row"] == 1


def test_wrong_pg_dump_version_is_refused(tmp_path):
    from tests.backup.conftest import _fake_pg
    from thresy.backup import pg

    tmp_path.joinpath("other").mkdir()
    other = _fake_pg(tmp_path / "other", 98, fail=False)
    with pytest.raises(pg.PgError, match=r"pg_dump for PostgreSQL 99 .* is 98\.99"):
        pg.find_tool("pg_dump", 99, other.bin_dir)
    assert pg.find_tool("pg_dump", 98, other.bin_dir).version == "98.99"


def test_interruption_is_recorded_without_an_event(conn, test_db, data, fake_pg):
    config = make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir)
    with pytest.raises(BackupInterrupted):
        run_backup(config, should_stop=lambda: True)
    assert core.get_status(conn)["last_error"].startswith("interrupted")
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_missing_memory_is_a_warning(conn, test_db, data, fake_pg):
    import shutil

    shutil.rmtree(data["workspace"] / "memory")
    result = run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    assert result.ok and result.memory_files == 0
    assert any("no memory/" in w for w in result.warnings)


def test_missing_scripts_is_no_warning(conn, test_db, data, fake_pg):
    import shutil

    shutil.rmtree(data["workspace"] / "scripts")
    result = run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    assert result.ok and result.scripts_files == 0 and result.warnings == []
    assert not any(name.endswith("scripts.tar.gz") for name in files(data["target"]))


def test_a_linked_workspace_directory_is_never_followed(conn, test_db, data, fake_pg, tmp_path):
    """The agent owns the workspace: a link must not make the backup read anything else."""
    import shutil

    secret = tmp_path / "secrets"
    secret.mkdir()
    (secret / "key.json").write_text("not for the backup\n")
    shutil.rmtree(data["workspace"] / "scripts")
    (data["workspace"] / "scripts").symlink_to(secret)
    result = run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    assert result.ok and result.scripts_files == 0
    assert any("scripts/ in the workspace is a link" in w for w in result.warnings)
    assert all(b"not for the backup" not in blob for blob in files(data["target"]).values())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_unreadable_memory_file_fails_the_backup(conn, test_db, data, fake_pg):
    secret = data["workspace"] / "memory" / "profile.md"
    secret.chmod(0)
    try:
        with pytest.raises(BackupError, match=r"cannot read memory/profile\.md"):
            run_backup(make_config(test_db, data, pg_bin_dir=fake_pg.bin_dir))
    finally:
        secret.chmod(0o644)


def test_cli_status_and_run(conn, test_db, data, fake_pg, monkeypatch):
    from thresy.cli import get_app
    from thresy.config import get_settings

    monkeypatch.chdir(data["workspace"].parent)
    for key, value in {
        "COACH_DATABASE_URL": test_db.app_url,
        "COACH_DATA_DIR": str(data["data"]),
        "COACH_WORKSPACE_DIR": str(data["workspace"]),
        "COACH_BACKUP_URL": data["target"].as_uri(),
        "COACH_BACKUP_PG_BIN_DIR": str(fake_pg.bin_dir),
        "COACH_TZ": "Europe/Berlin",
        "COACH_BACKUP_WEEKDAYS": "Sun",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        before = runner.invoke(get_app(), ["backup", "status"])
        assert before.exit_code == 0, before.output
        assert "last success: never" in before.output
        assert "schedule: sun at 03:30 (Europe/Berlin); next run 20" in before.output
        planned = json.loads(runner.invoke(get_app(), ["backup", "status", "--json"]).output)
        next_run = datetime.fromisoformat(planned["next_run_at"])
        assert (next_run.weekday(), f"{next_run:%H:%M}") == (6, "03:30")
        assert next_run.utcoffset() in (timedelta(hours=1), timedelta(hours=2))
        done = runner.invoke(get_app(), ["backup", "run", "--json"])
        assert done.exit_code == 0, done.output
        assert json.loads(done.output)["fit_uploaded"] == 2
        after = runner.invoke(get_app(), ["backup", "status"])
        assert "last success: 20" in after.output and "2 new FIT files" in after.output
        assert "2 memory files, 2 kept scripts, 2 new FIT files" in after.output
        monkeypatch.setenv("COACH_BACKUP_URL", "ftp://nowhere")
        bad = runner.invoke(get_app(), ["backup", "run"])
        assert bad.exit_code == 1 and "file:// or gs://" in bad.output
        monkeypatch.setenv("COACH_BACKUP_WEEKDAYS", "sun,funday")
        refused = runner.invoke(get_app(), ["backup", "schedule"])
        assert refused.exit_code == 1 and "unknown weekday 'funday'" in refused.output
    finally:
        get_settings.cache_clear()


def test_status_of_a_record_from_before_kept_scripts():
    from thresy.backup.cli import _status_text

    old = {
        "last_success_at": "2030-01-02T03:30:00+00:00",
        "last_run_id": "r1",
        "last_result": {"objects": 3, "bytes": 2e6, "memory_files": 6, "fit_uploaded": 1},
    }
    assert "(run r1, 3 objects, 2.0 MB, 6 memory files, 1 new FIT files)" in _status_text(old)


# -- real pg_dump and pg_restore ------------------------------------------------------------------


def _no_scratch_left(conn) -> bool:
    rows = conn.execute(
        "SELECT count(*) AS n FROM pg_database WHERE datname LIKE 'thresy\\_restore\\_check\\_%'"
    ).fetchone()
    return rows["n"] == 0


def test_real_backup_with_restore_check(conn, test_db, data, real_pg_bin, createdb):
    config = make_config(test_db, data, pg_bin_dir=real_pg_bin)
    result = run_backup(config, restore_check=True)
    assert result.ok, result.text()
    check = result.restore_check
    assert check is not None and check.ok
    by_table = {t.table: t for t in check.tables}
    assert by_table["activities"].restored == 2
    assert by_table["schema_migrations"].restored == by_table["schema_migrations"].expected > 0
    assert _no_scratch_left(conn)
    assert core.get_status(conn)["restore_check"]["ok"] is True

    # The stored run directory restores too, compared with its manifest.
    stored = restore_check(config, run_dir=run_dir(data["target"]))
    assert stored.ok, stored.text()
    assert stored.source == f"run {result.run_id}"
    assert _no_scratch_left(conn)


def test_real_restore_check_of_a_fresh_dump(conn, test_db, data, real_pg_bin, createdb):
    config = make_config(test_db, data, pg_bin_dir=real_pg_bin, target=None)
    result = restore_check(config)
    assert result.ok, result.text()
    assert result.source == "a fresh dump"
    assert "PASS" in result.text()


def test_real_restore_check_detects_a_damaged_dump(conn, test_db, data, real_pg_bin, createdb):
    config = make_config(test_db, data, pg_bin_dir=real_pg_bin)
    run_backup(config)
    directory = run_dir(data["target"])
    dump = directory / "thresy.dump"
    damaged = dump.read_bytes()
    dump.chmod(0o644)
    dump.write_bytes(damaged[: len(damaged) // 2])
    result = restore_check(config, run_dir=directory)
    assert not result.ok and "pg_restore failed" in result.detail
    assert _no_scratch_left(conn)


def test_restore_check_without_createdb_explains(conn, test_db, data, real_pg_bin):
    has = conn.execute("SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
    if has["rolcreatedb"]:
        pytest.skip("coach_app has CREATEDB on this server")
    config = make_config(test_db, data, pg_bin_dir=real_pg_bin, target=None)
    with pytest.raises(BackupError, match="CREATEDB"):
        restore_check(config)


def test_real_encrypted_run_restores_with_the_key(
    conn, test_db, data, real_pg_bin, real_age, createdb, tmp_path
):
    import subprocess

    age, keygen = real_age
    key = tmp_path / "key.txt"
    subprocess.run([keygen, "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        line.split(": ", 1)[1]
        for line in key.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    config = make_config(test_db, data, pg_bin_dir=real_pg_bin, recipients=[recipient], age_bin=age)
    run_backup(config)
    directory = run_dir(data["target"])
    with pytest.raises(BackupError, match="--identity"):
        restore_check(config, run_dir=directory)
    result = restore_check(config, run_dir=directory, identity=key)
    assert result.ok, result.text()
