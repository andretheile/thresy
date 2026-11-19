"""Original activity files: extract the FIT, save it under ``COACH_DATA_DIR``, store records.

Layout (contract, "Files"): ``fit/<YYYY>/<activity_id>.fit`` where ``YYYY`` is the year of the
activity's local start; ``activities.fit_path`` holds that relative path.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import zipfile
from datetime import date, datetime
from pathlib import Path

from thresy import store
from thresy.fit import FitError, fit_bytes_from_zip, parse_fit
from thresy.store import Conn


def extract_fit(original: bytes) -> bytes:
    """The FIT file inside Garmin's "original" download (a zip archive, or a bare FIT file)."""
    if original[:2] == b"PK":
        try:
            return fit_bytes_from_zip(original)
        except zipfile.BadZipFile as exc:
            raise FitError(f"broken zip archive: {exc}") from exc
    if len(original) >= 12 and original[8:12] == b".FIT":
        return original
    raise FitError("the download is neither a zip archive nor a FIT file")


def fit_relpath(activity_id: int, local_start: datetime | date) -> str:
    return f"fit/{local_start.year:04d}/{int(activity_id)}.fit"


def save_file(data_dir: Path, relpath: str, data: bytes) -> Path:
    """Write ``data`` atomically to ``data_dir/relpath`` (readable by the bridge, 0644)."""
    path = Path(data_dir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def store_original(
    conn: Conn,
    data_dir: Path,
    activity_id: int,
    local_start: datetime | date,
    original: bytes,
) -> int:
    """Save the FIT file of ``original`` and replace the activity's records and laps.

    Returns the number of records. Raises :class:`FitError` when there is no FIT file in
    ``original``, or when the saved file cannot be parsed (``fit_path`` is then still set, so
    the file can be parsed again later, and ``has_records`` stays false).
    """
    fit_bytes = extract_fit(original)
    relpath = fit_relpath(activity_id, local_start)
    save_file(data_dir, relpath, fit_bytes)
    try:
        parsed = parse_fit(fit_bytes)
    except FitError:
        store.replace_activity_records(conn, activity_id, [], [], fit_path=relpath)
        raise
    return store.replace_activity_records(
        conn, activity_id, parsed.records, parsed.laps, fit_path=relpath
    )
