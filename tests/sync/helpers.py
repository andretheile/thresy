"""Helpers for the sync tests: config, a controllable clock, synthetic originals."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tests import synthetic
from tests.fitbuilder import LAP, RECORD, SESSION, FitBuilder, fit_time
from thresy.sync.config import SyncConfig


def make_config(tmp_path: Path, **overrides: Any) -> SyncConfig:
    values: dict[str, Any] = {
        "zone": ZoneInfo("UTC"),
        "data_dir": tmp_path / "data",
        "token_dir": tmp_path / "garmin",
        "jitter_s": 0,
        "backfill_pause_s": 0,
    }
    values.update(overrides)
    return SyncConfig(**values)


def fit_file(start: datetime, *, seconds: int = 30, sport: int = 1) -> bytes:
    """A small synthetic activity FIT file: one record per second and one lap."""
    b = FitBuilder()
    t0 = fit_time(start)
    for i in range(seconds):
        b.message(RECORD, {253: t0 + i, 5: i * 300, 73: 3000, 3: 120 + i % 10})
    end = t0 + seconds - 1
    b.message(LAP, {254: 0, 253: end, 2: t0, 7: seconds * 1000, 8: seconds * 1000, 25: sport})
    b.message(SESSION, {253: end, 2: t0, 5: sport, 6: 0})
    return b.build()


def zipped(fit: bytes, name: str = "activity.fit") -> bytes:
    """Wrap a FIT file the way Garmin's "original" download does."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, fit)
    return buffer.getvalue()


def activity_at(activity_id: int, start_utc: datetime, **extra: Any) -> dict[str, Any]:
    """A synthetic summary starting at ``start_utc`` (local time = UTC in these tests)."""
    stamp = start_utc.strftime("%Y-%m-%d %H:%M:%S")
    return synthetic.activity(activity_id, start_local=stamp, start_gmt=stamp, **extra)


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2030, 1, 2, 6, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class StaticProvider:
    """A ClientSource that always hands out the same client."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.invalidated = 0
        self.error: BaseException | None = None

    def get(self, conn: Any, now: datetime) -> Any:
        if self.error is not None:
            raise self.error
        return self.client

    def invalidate(self) -> None:
        self.invalidated += 1
