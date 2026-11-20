"""Backups: a Postgres dump, the original FIT files and the coach's memory files and kept
scripts, written to a local directory or a Google Cloud Storage bucket with a create-only
credential, optionally encrypted with age. See docs/backup.md.
"""

from .core import (
    BackupConfig,
    BackupError,
    BackupResult,
    RestoreCheckResult,
    restore_check,
    run_backup,
)
from .targets import GcsTarget, LocalTarget, TargetError, open_target

__all__ = [
    "BackupConfig",
    "BackupError",
    "BackupResult",
    "GcsTarget",
    "LocalTarget",
    "RestoreCheckResult",
    "TargetError",
    "open_target",
    "restore_check",
    "run_backup",
]
