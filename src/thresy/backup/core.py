"""``coach backup run``, ``restore-check`` and ``status``.

One run:

1. Opens a read-only REPEATABLE READ transaction, exports its snapshot and counts the rows of
   every table in it; ``pg_dump`` then dumps exactly that snapshot, so the counts describe the
   dump.
2. Uploads ``runs/<run id>/thresy.dump``, ``runs/<run id>/memory.tar.gz`` (the workspace's
   ``memory/``), ``runs/<run id>/scripts.tar.gz`` (the analysis scripts the coach keeps in
   ``scripts/``, when that directory exists; runs before it was added have none), every FIT
   file not uploaded before as ``fit/<YYYY>/<id>.fit``, and last ``runs/<run id>/manifest.json``
   (row counts, versions, objects with sizes and SHA-256). With age recipients configured, each
   file is encrypted first and gets the suffix ``.age``. Links in the workspace are skipped,
   never followed.
3. Records the outcome in ``sync_state`` (``backup_status``; the FIT files already uploaded in
   ``backup_fit``). A failure also raises a ``backup_failed`` event (once per UTC day) that the
   bridge reports to the athlete.

Nothing secret is backed up: the dump holds the database (no secrets are stored in tables),
the memory files and kept scripts are the agent's notes and code, FIT files are activity
recordings. Tokens, keys and ``.env`` are never read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import secrets
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thresy import db, store
from thresy.logs import redact

from . import crypto, pg
from .targets import Target, TargetError

log = logging.getLogger(__name__)

STATUS_KEY = "backup_status"
FIT_KEY = "backup_fit"
EVENT_KIND = "backup_failed"
SCRATCH_PREFIX = "thresy_restore_check_"
DUMP_NAME = "thresy.dump"
MEMORY_NAME = "memory.tar.gz"
SCRIPTS_NAME = "scripts.tar.gz"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
FIT_STATE_SAVE_EVERY = 25


class BackupError(RuntimeError):
    """A backup or restore check failed."""


class BackupInterrupted(BackupError):
    """The service was asked to stop during a run."""


@dataclass
class BackupConfig:
    database_url: str
    data_dir: Path
    workspace_dir: Path
    target: Target | None
    """Where backups go; None for a restore check of a downloaded run."""
    recipients: list[str] = field(default_factory=list)
    age_bin: str = "age"
    pg_bin_dir: Path | None = None
    work_dir: Path | None = None

    @property
    def encrypted(self) -> bool:
        return bool(self.recipients)

    def suffix(self) -> str:
        return ".age" if self.encrypted else ""


@dataclass
class TableCheck:
    table: str
    expected: int | None
    restored: int | None
    live: int | None

    @property
    def ok(self) -> bool:
        return self.expected is not None and self.restored == self.expected


@dataclass
class RestoreCheckResult:
    ok: bool
    source: str
    detail: str
    tables: list[TableCheck] = field(default_factory=list)
    duration_s: float = 0.0
    pg_restore: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 1),
            "pg_restore": self.pg_restore,
            "tables": [
                {"table": t.table, "expected": t.expected, "restored": t.restored, "live": t.live}
                for t in self.tables
            ],
        }

    def text(self) -> str:
        lines = [f"restore check of {self.source}: {'PASS' if self.ok else 'FAIL'}"]
        if self.tables:
            width = max(len(t.table) for t in self.tables)
            lines.append(f"  {'table':<{width}}  {'in dump':>10}  {'restored':>10}  {'live':>10}")
            for t in self.tables:
                mark = "" if t.ok else "  <- differs"
                lines.append(
                    f"  {t.table:<{width}}  {_n(t.expected):>10}  {_n(t.restored):>10}  "
                    f"{_n(t.live):>10}{mark}"
                )
        lines.append(f"  {self.detail} ({self.duration_s:.0f} s)")
        return "\n".join(lines)


def _n(value: int | None) -> str:
    return "-" if value is None else str(value)


@dataclass
class BackupResult:
    run_id: str
    started_at: datetime
    objects: list[dict[str, Any]] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    fit_uploaded: int = 0
    fit_present: int = 0
    fit_total: int = 0
    memory_files: int = 0
    scripts_files: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    restore_check: RestoreCheckResult | None = None
    duration_s: float = 0.0
    versions: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and (self.restore_check is None or self.restore_check.ok)

    @property
    def bytes(self) -> int:
        return sum(int(o["bytes"]) for o in self.objects if o.get("created"))

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "objects": len(self.objects),
            "bytes": self.bytes,
            "fit_uploaded": self.fit_uploaded,
            "fit_present": self.fit_present,
            "fit_total": self.fit_total,
            "memory_files": self.memory_files,
            "scripts_files": self.scripts_files,
            "duration_s": round(self.duration_s, 1),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            **self.summary(),
            "row_counts": self.row_counts,
            "warnings": self.warnings,
            "errors": self.errors,
            "restore_check": self.restore_check.as_dict() if self.restore_check else None,
            "objects_list": self.objects,
        }

    def text(self) -> str:
        lines = [
            f"backup {self.run_id}: {'ok' if self.ok else 'FAILED'} in {self.duration_s:.0f} s",
            f"  database: {sum(self.row_counts.values())} rows in {len(self.row_counts)} tables",
            f"  memory files: {self.memory_files}, kept scripts: {self.scripts_files}",
            f"  FIT files: {self.fit_uploaded} uploaded, {self.fit_present} already there, "
            f"{self.fit_total} in total",
            f"  objects created: {sum(1 for o in self.objects if o.get('created'))}, "
            f"{self.bytes / 1e6:.1f} MB",
        ]
        lines += [f"  warning: {w}" for w in self.warnings]
        lines += [f"  error: {e}" for e in self.errors]
        if self.restore_check is not None:
            lines.append(self.restore_check.text())
        return "\n".join(lines)


# -- state ----------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def get_status(conn: store.Conn) -> dict[str, Any]:
    value = store.get_sync_state(conn, STATUS_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


def _update_status(conn: store.Conn, **changes: Any) -> dict[str, Any]:
    status = get_status(conn)
    status.update(changes)
    store.set_sync_state(conn, STATUS_KEY, status)
    return status


def _fit_identity(config: BackupConfig) -> str:
    assert config.target is not None
    return f"{config.target.url}|encrypted={config.encrypted}"


def _load_fit_state(conn: store.Conn, config: BackupConfig) -> dict[str, str]:
    value = store.get_sync_state(conn, FIT_KEY, {})
    if not isinstance(value, dict) or value.get("target") != _fit_identity(config):
        return {}
    files = value.get("files")
    return dict(files) if isinstance(files, dict) else {}


def _save_fit_state(conn: store.Conn, config: BackupConfig, files: dict[str, str]) -> None:
    store.set_sync_state(conn, FIT_KEY, {"target": _fit_identity(config), "files": files})


def record_failure(config: BackupConfig, error: str, *, notify: bool = True) -> None:
    """Record a failed run and raise ``backup_failed`` (at most one per UTC day). The error is
    redacted first: the event's text is sent to the athlete."""
    error = redact(error)
    try:
        with db.connect(config.database_url) as conn:
            status = get_status(conn)
            now = _now()
            _update_status(
                conn,
                last_attempt_at=now.isoformat(),
                last_error=error,
                last_error_at=now.isoformat(),
                running_run_id=None,
                failures_in_row=int(status.get("failures_in_row") or 0) + 1,
            )
            if notify:
                store.insert_event(
                    conn,
                    EVENT_KIND,
                    {"error": error[:500], "last_success_at": status.get("last_success_at")},
                    dedupe_key=f"{EVENT_KIND}:{now.date().isoformat()}",
                )
    except Exception:
        log.exception("could not record the backup failure in the database")


# -- the parts ------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_dump(url: str, tool: pg.Tool, target: Path) -> dict[str, int]:
    """Dump the database of ``url`` into ``target``; returns the row counts of the dumped
    snapshot."""
    with db.connect(url) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        row = conn.execute("SELECT pg_export_snapshot() AS s").fetchone()
        snapshot = row["s"]
        counts = pg.row_counts(conn)
        pg.dump(tool, url, target, snapshot=snapshot)
    return counts


def workspace_archive(workspace: Path, name: str, target: Path) -> int:
    """``<name>/`` of the workspace (``memory`` or ``scripts``) as a gzip'd tar; returns the
    number of files. Symlinks and special files are skipped, never followed; an unreadable file
    is an error."""
    root = workspace / name
    count = 0
    with tarfile.open(target, "w:gz") as archive:
        archive.add(root, arcname=name, recursive=False)
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(workspace).as_posix()
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                continue
            try:
                archive.add(path, arcname=rel, recursive=False)
            except PermissionError as exc:
                raise BackupError(f"cannot read {rel}: permission denied") from exc
            count += path.is_file()
    return count


def fit_files(data_dir: Path) -> Iterator[tuple[str, Path]]:
    root = data_dir / "fit"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".fit":
            yield path.relative_to(data_dir).as_posix(), path


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


class _Uploader:
    def __init__(self, config: BackupConfig, work: Path, result: BackupResult) -> None:
        self.config = config
        self.work = work
        self.result = result

    def put(self, name: str, source: Path, *, record: bool = True) -> bool:
        """Encrypt (if configured) and upload ``source`` as ``name`` (+ ``.age``)."""
        upload = source
        if self.config.encrypted:
            upload = self.work / f"upload-{secrets.token_hex(6)}.age"
            crypto.encrypt(source, upload, self.config.recipients, binary=self.config.age_bin)
        object_name = name + self.config.suffix()
        assert self.config.target is not None
        try:
            created = self.config.target.put(object_name, upload)
            if record:
                self.result.objects.append(
                    {
                        "name": object_name,
                        "bytes": upload.stat().st_size,
                        "sha256": _sha256(upload),
                        "created": created,
                    }
                )
        finally:
            if upload != source:
                upload.unlink(missing_ok=True)
        return created


def _upload_fit(
    config: BackupConfig,
    uploader: _Uploader,
    result: BackupResult,
    should_stop: Callable[[], bool],
) -> None:
    with db.connect(config.database_url) as conn:
        done = _load_fit_state(conn, config)
        pending = 0
        try:
            for rel, path in fit_files(config.data_dir):
                result.fit_total += 1
                try:
                    fingerprint = _fingerprint(path)
                except OSError as exc:
                    result.errors.append(f"{rel}: {exc.strerror or exc}")
                    continue
                if done.get(rel) == fingerprint:
                    continue
                if should_stop():
                    raise BackupInterrupted("stopped while uploading FIT files")
                try:
                    created = uploader.put(rel, path, record=False)
                except PermissionError as exc:
                    result.errors.append(f"{rel}: {exc.strerror or exc}")
                    continue
                if created:
                    result.fit_uploaded += 1
                else:
                    result.fit_present += 1
                done[rel] = fingerprint
                pending += 1
                if pending >= FIT_STATE_SAVE_EVERY:
                    _save_fit_state(conn, config, done)
                    pending = 0
        finally:
            if pending:
                _save_fit_state(conn, config, done)


# -- run ------------------------------------------------------------------------------------------


def new_run_id(now: datetime | None = None) -> str:
    moment = (now or _now()).astimezone(UTC)
    return f"{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def run_backup(
    config: BackupConfig,
    *,
    restore_check: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> BackupResult:
    """One backup run. Raises BackupError (after recording the failure and raising the
    ``backup_failed`` event) when anything failed; returns the result otherwise."""
    if config.target is None:
        raise BackupError("COACH_BACKUP_URL is not set")
    started = _now()
    clock = time.monotonic()
    result = BackupResult(run_id=new_run_id(started), started_at=started)
    work = Path(tempfile.mkdtemp(prefix="thresy-backup-", dir=config.work_dir))
    work.chmod(0o700)
    prefix = f"runs/{result.run_id}/"
    try:
        with db.connect(config.database_url) as conn:
            _update_status(conn, last_attempt_at=started.isoformat(), running_run_id=result.run_id)
            major = pg.server_major(conn)
            result.versions["server"] = pg.server_version(conn)
        dump_tool = pg.find_tool("pg_dump", major, config.pg_bin_dir)
        result.versions["pg_dump"] = dump_tool.version
        uploader = _Uploader(config, work, result)

        log.info("backup %s: dumping the database", result.run_id)
        dump_path = work / DUMP_NAME
        result.row_counts = make_dump(config.database_url, dump_tool, dump_path)
        log.info(
            "backup %s: uploading the dump (%d bytes)", result.run_id, dump_path.stat().st_size
        )
        uploader.put(prefix + DUMP_NAME, dump_path)
        if should_stop():
            raise BackupInterrupted("stopped after the dump")

        for name, object_name in (("memory", MEMORY_NAME), ("scripts", SCRIPTS_NAME)):
            root = config.workspace_dir / name
            if root.is_symlink():  # never followed: it could point at a secret
                result.warnings.append(f"{name}/ in the workspace is a link; not backed up")
            elif root.is_dir():
                archive = work / object_name
                count = workspace_archive(config.workspace_dir, name, archive)
                if name == "memory":
                    result.memory_files = count
                else:
                    result.scripts_files = count
                uploader.put(prefix + object_name, archive)
            elif name == "memory":  # a missing scripts/ only means none were kept yet
                result.warnings.append(f"no memory/ in the workspace {config.workspace_dir}")

        log.info("backup %s: uploading new FIT files", result.run_id)
        _upload_fit(config, uploader, result, should_stop)

        if restore_check:
            log.info("backup %s: restore check of the dump", result.run_id)
            result.restore_check = verify_dump(
                config, dump_path, result.row_counts, source=f"the dump of run {result.run_id}"
            )
        result.duration_s = time.monotonic() - clock
        manifest = work / MANIFEST_NAME
        manifest.write_text(json.dumps(_manifest(config, result), indent=2, default=str))
        uploader.put(prefix + MANIFEST_NAME, manifest, record=False)
    except BackupInterrupted as exc:
        record_failure(config, f"interrupted: {exc}", notify=False)
        raise
    except (BackupError, pg.PgError, crypto.CryptoError, TargetError, OSError) as exc:
        message = str(exc) if not isinstance(exc, OSError) else f"{exc.strerror or exc}"
        record_failure(config, message)
        raise BackupError(message) from exc
    except Exception as exc:  # a bug, a lost database connection, ...
        record_failure(config, f"{type(exc).__name__}: {exc}")
        raise BackupError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result.duration_s = time.monotonic() - clock
    if not result.ok:
        problems = result.errors[:3]
        if result.restore_check is not None and not result.restore_check.ok:
            problems.append(f"restore check failed: {result.restore_check.detail}")
            with contextlib.suppress(Exception), db.connect(config.database_url) as conn:
                _update_status(conn, restore_check=_check_state(result.restore_check, _now()))
        message = "; ".join(problems)
        record_failure(config, message)
        raise BackupError(message)
    finished = _now()
    with db.connect(config.database_url) as conn:
        changes: dict[str, Any] = {
            "last_success_at": finished.isoformat(),
            "last_run_id": result.run_id,
            "last_result": result.summary(),
            "failures_in_row": 0,
            "running_run_id": None,
            "target": config.target.url,
            "encrypted": config.encrypted,
        }
        if result.restore_check is not None:
            changes["restore_check"] = _check_state(result.restore_check, finished)
        _update_status(conn, **changes)
    return result


def _manifest(config: BackupConfig, result: BackupResult) -> dict[str, Any]:
    return {
        "format": MANIFEST_FORMAT,
        "run_id": result.run_id,
        "started_at": result.started_at.isoformat(),
        "encrypted": config.encrypted,
        "recipients": config.recipients,
        "versions": result.versions,
        "row_counts": result.row_counts,
        "objects": result.objects,
        "fit": {
            "uploaded": result.fit_uploaded,
            "already_there": result.fit_present,
            "total": result.fit_total,
            "prefix": "fit/",
        },
        "memory_files": result.memory_files,
        "scripts_files": result.scripts_files,
        "restore_check": result.restore_check.as_dict() if result.restore_check else None,
        "warnings": result.warnings,
        "errors": result.errors,
    }


def _check_state(check: RestoreCheckResult, at: datetime) -> dict[str, Any]:
    return {"at": at.isoformat(), "ok": check.ok, "source": check.source, "detail": check.detail}


# -- restore check --------------------------------------------------------------------------------


def _drop_leftovers(conn: store.Conn) -> None:
    rows = conn.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE %s"
        " AND pg_get_userbyid(datdba) = current_user",
        (SCRATCH_PREFIX.replace("_", r"\_") + "%",),
    ).fetchall()
    for row in rows:
        log.info("dropping a scratch database left over from an earlier check: %s", row["datname"])
        pg.drop_database(conn, row["datname"])


def verify_dump(
    config: BackupConfig,
    dump_path: Path,
    expected: dict[str, int],
    *,
    source: str,
    privileges: bool = True,
    live: bool = True,
) -> RestoreCheckResult:
    """Restore ``dump_path`` into a scratch database on the server of the configured URL,
    compare its row counts with ``expected``, and drop it."""
    clock = time.monotonic()
    scratch = f"{SCRATCH_PREFIX}{_now():%Y%m%d%H%M%S}_{secrets.token_hex(2)}"
    with db.connect(config.database_url) as conn:
        major = pg.server_major(conn)
        tool = pg.find_tool("pg_restore", major, config.pg_bin_dir)
        live_counts = pg.row_counts(conn) if live else {}
        _drop_leftovers(conn)
        pg.create_database(conn, scratch)
        try:
            try:
                pg.restore(tool, config.database_url, scratch, dump_path, privileges=privileges)
            except pg.PgError as exc:
                return RestoreCheckResult(
                    ok=False,
                    source=source,
                    detail=str(exc),
                    duration_s=time.monotonic() - clock,
                    pg_restore=tool.version,
                )
            with db.connect(pg.with_dbname(config.database_url, scratch)) as scratch_conn:
                restored = pg.row_counts(scratch_conn)
        finally:
            pg.drop_database(conn, scratch)
    names = sorted(set(expected) | set(restored))
    tables = [
        TableCheck(n, expected.get(n), restored.get(n), live_counts.get(n) if live else None)
        for n in names
    ]
    bad = [t.table for t in tables if not t.ok]
    rows = sum(restored.values())
    detail = (
        f"all {len(tables)} tables restored with the expected row counts ({rows} rows)"
        if not bad
        else f"row counts differ in {', '.join(bad)}"
    )
    return RestoreCheckResult(
        ok=not bad,
        source=source,
        detail=detail,
        tables=tables,
        duration_s=time.monotonic() - clock,
        pg_restore=tool.version,
    )


def _find(run_dir: Path, name: str) -> Path | None:
    for candidate in (run_dir / name, run_dir / f"{name}.age"):
        if candidate.is_file():
            return candidate
    return None


def _plain(path: Path, work: Path, identity: Path | None, age_bin: str) -> Path:
    if path.suffix != ".age":
        return path
    if identity is None:
        raise BackupError(
            f"{path.name} is encrypted: pass --identity with the age private key (it is never "
            "kept on the server, so run this where the key is)"
        )
    target = work / path.name.removesuffix(".age")
    crypto.decrypt(path, target, identity, binary=age_bin)
    return target


def restore_check(
    config: BackupConfig,
    *,
    run_dir: Path | None = None,
    identity: Path | None = None,
    privileges: bool = True,
    live: bool = True,
    record: bool = True,
) -> RestoreCheckResult:
    """Prove that a dump restores. Without ``run_dir``: a fresh dump made like a backup run's
    (the bucket cannot be read with the backup's credential). With ``run_dir``: the dump of a
    downloaded (or local) run directory, compared with its manifest."""
    work = Path(tempfile.mkdtemp(prefix="thresy-restore-check-", dir=config.work_dir))
    work.chmod(0o700)
    try:
        if run_dir is None:
            with db.connect(config.database_url) as conn:
                major = pg.server_major(conn)
            tool = pg.find_tool("pg_dump", major, config.pg_bin_dir)
            dump_path = work / DUMP_NAME
            expected = make_dump(config.database_url, tool, dump_path)
            source = "a fresh dump"
        else:
            dump_file = _find(run_dir, DUMP_NAME)
            manifest_file = _find(run_dir, MANIFEST_NAME)
            if dump_file is None or manifest_file is None:
                raise BackupError(f"{run_dir} has no {DUMP_NAME}[.age] and {MANIFEST_NAME}[.age]")
            dump_path = _plain(dump_file, work, identity, config.age_bin)
            manifest = json.loads(_plain(manifest_file, work, identity, config.age_bin).read_text())
            expected = {k: int(v) for k, v in (manifest.get("row_counts") or {}).items()}
            source = f"run {manifest.get('run_id', run_dir.name)}"
        result = verify_dump(
            config, dump_path, expected, source=source, privileges=privileges, live=live
        )
    except (pg.PgError, crypto.CryptoError, OSError, ValueError) as exc:
        raise BackupError(str(exc)) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if record:
        with contextlib.suppress(Exception), db.connect(config.database_url) as conn:
            _update_status(conn, restore_check=_check_state(result, _now()))
    return result
