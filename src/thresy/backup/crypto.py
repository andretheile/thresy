"""Encryption with ``age`` (https://age-encryption.org), through its command-line tool.

Files are encrypted to one or more public keys (recipients) before they are uploaded. The
private key is needed only to restore and never lives on the server.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path


class CryptoError(RuntimeError):
    """age is missing or failed."""


def _age(binary: str) -> str:
    found = shutil.which(binary)
    if found is None:
        raise CryptoError(
            f"the age tool was not found ({binary}); install age or unset "
            "COACH_BACKUP_AGE_RECIPIENT"
        )
    return found


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600, check=False)
    if proc.returncode != 0:
        raise CryptoError(f"age failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")


def encrypt(source: Path, target: Path, recipients: Sequence[str], *, binary: str = "age") -> None:
    """Encrypt ``source`` to ``target`` for ``recipients`` (public keys)."""
    if not recipients:
        raise CryptoError("no age recipient given")
    argv = [_age(binary)]
    for recipient in recipients:
        argv += ["--recipient", recipient]
    _run([*argv, "--output", str(target), str(source)])


def decrypt(source: Path, target: Path, identity: Path, *, binary: str = "age") -> None:
    """Decrypt ``source`` to ``target`` with the private key file ``identity``."""
    _run(
        [
            _age(binary),
            "--decrypt",
            "--identity",
            str(identity),
            "--output",
            str(target),
            str(source),
        ]
    )
