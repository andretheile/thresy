"""``coach backup run|restore-check|status|schedule``: the typer plugin of the backup."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from thresy.cli import JsonOption, emit, fail

app = typer.Typer(
    help="Backups of the database, FIT files, memory files and kept scripts.", no_args_is_help=True
)


@app.callback()
def _group() -> None:
    """Backups of the database, FIT files, memory files and kept scripts (docs/backup.md)."""


def _config(*, need_target: bool, client: Any = None):
    from thresy.config import get_settings

    from .core import BackupConfig
    from .settings import BackupSettings
    from .targets import open_target

    settings = get_settings()
    bs = BackupSettings()
    target = None
    if bs.backup_url:
        target = open_target(
            bs.backup_url, gcs_credentials=bs.backup_gcs_credentials, client=client
        )
    elif need_target:
        from thresy.config import ConfigError

        raise ConfigError("COACH_BACKUP_URL is not set (file:///path or gs://bucket/prefix)")
    config = BackupConfig(
        database_url=settings.require_database_url(),
        data_dir=Path(settings.data_dir).expanduser(),
        workspace_dir=Path(settings.workspace_dir).expanduser(),
        target=target,
        recipients=bs.recipients,
        age_bin=bs.backup_age_bin,
        pg_bin_dir=bs.backup_pg_bin_dir,
        work_dir=bs.backup_work_dir,
    )
    return config, settings, bs


def _errors():
    from thresy.config import ConfigError

    from .core import BackupError
    from .targets import TargetError

    return (BackupError, ConfigError, TargetError, ValueError)


@app.command("run")
def run(
    restore_check: Annotated[
        bool,
        typer.Option(
            "--restore-check", help="Also restore the dump into a scratch database and compare."
        ),
    ] = False,
    json_out: JsonOption = False,
) -> None:
    """Back up now: database dump, memory files, kept scripts and new FIT files to
    COACH_BACKUP_URL."""
    from .core import run_backup

    try:
        config, _, _ = _config(need_target=True)
        result = run_backup(config, restore_check=restore_check)
    except _errors() as exc:
        raise fail(f"backup failed: {exc}", as_json=json_out) from exc
    emit(result.as_dict(), as_json=json_out, text=result.text())


@app.command("restore-check")
def restore_check(
    run_dir: Annotated[
        Path | None,
        typer.Option(
            "--dir",
            help="A run directory (downloaded from the bucket, or under a file:// target) "
            "instead of a fresh dump.",
        ),
    ] = None,
    identity: Annotated[
        Path | None,
        typer.Option("--identity", help="age private key file, for encrypted run directories."),
    ] = None,
    no_privileges: Annotated[
        bool,
        typer.Option(
            "--no-privileges",
            help="Skip GRANTs (for a server without the coach_agent and coach_ro roles).",
        ),
    ] = False,
    no_live: Annotated[
        bool,
        typer.Option("--no-live", help="Do not count the rows of COACH_DATABASE_URL's database."),
    ] = False,
    json_out: JsonOption = False,
) -> None:
    """Prove a dump restores: restore it into a scratch database on the server of
    COACH_DATABASE_URL (the role needs CREATEDB), compare row counts, drop it."""
    from .core import restore_check as check

    try:
        config, _, _ = _config(need_target=False)
        result = check(
            config,
            run_dir=run_dir,
            identity=identity,
            privileges=not no_privileges,
            live=not no_live,
            record=run_dir is None,
        )
    except _errors() as exc:
        raise fail(f"restore check failed: {exc}", as_json=json_out) from exc
    emit(result.as_dict(), as_json=json_out, text=result.text())
    if not result.ok:
        raise typer.Exit(1)


@app.command("status")
def status(json_out: JsonOption = False) -> None:
    """Last backup, last failure, last restore check."""
    from thresy import db

    from .core import get_status

    try:
        config, _, bs = _config(need_target=False)
        with db.connect(config.database_url) as conn:
            data = get_status(conn)
    except (*_errors(), OSError) as exc:
        raise fail(str(exc), as_json=json_out) from exc
    except Exception as exc:  # psycopg.OperationalError and friends
        raise fail(f"database: {exc}", as_json=json_out) from exc
    data = {
        "configured_target": bs.backup_url,
        "encrypted": bool(bs.recipients),
        **data,
    }
    emit(data, as_json=json_out, text=_status_text(data))


def _status_text(data: dict[str, Any]) -> str:
    target = data.get("configured_target") or "not configured (COACH_BACKUP_URL)"
    lines = [f"target: {target}{' (encrypted with age)' if data.get('encrypted') else ''}"]
    if data.get("last_success_at"):
        result = data.get("last_result") or {}
        workspace = ""
        if "memory_files" in result:  # older records lack the counts
            workspace = f"{result['memory_files']} memory files, "
        if "scripts_files" in result:
            workspace += f"{result['scripts_files']} kept scripts, "
        lines.append(
            f"last success: {data['last_success_at']} (run {data.get('last_run_id')}, "
            f"{result.get('objects', '?')} objects, {int(result.get('bytes') or 0) / 1e6:.1f} MB, "
            f"{workspace}{result.get('fit_uploaded', 0)} new FIT files)"
        )
    else:
        lines.append("last success: never")
    if data.get("last_attempt_at"):
        lines.append(f"last attempt: {data['last_attempt_at']}")
    if int(data.get("failures_in_row") or 0):
        lines.append(
            f"FAILING: {data['failures_in_row']} run(s) in a row; last error "
            f"({data.get('last_error_at')}): {data.get('last_error')}"
        )
    check = data.get("restore_check")
    if isinstance(check, dict):
        lines.append(
            f"restore check: {check.get('at')} {'ok' if check.get('ok') else 'FAILED'} "
            f"({check.get('source')}: {check.get('detail')})"
        )
    else:
        lines.append("restore check: never")
    return "\n".join(lines)


@app.command("schedule")
def schedule() -> None:
    """Run as a service: a backup every night at COACH_BACKUP_AT (local time)."""
    import threading

    from . import schedule as service

    try:
        config, settings, bs = _config(need_target=True)
    except _errors() as exc:
        raise fail(str(exc)) from exc
    stop = threading.Event()
    service.install_signal_handlers(stop)
    service.run_forever(config, bs, settings.zone, stop=stop)
