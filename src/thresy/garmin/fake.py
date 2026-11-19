"""A :class:`~thresy.garmin.client.GarminClient` that serves canned payloads.

Used by the tests and usable by anything that wants to push stored Garmin payloads through the
sync code (for example the legacy importer)::

    fake = FakeGarminClient()
    fake.add_activity(summary, original=zip_bytes)
    fake.set_wellness(day, sleep=..., hrv=..., stats=...)
    run_pass(conn, fake, config)

Every call is recorded in :attr:`FakeGarminClient.calls`. :meth:`fail` makes a method raise,
for error-path tests.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from thresy.garmin.errors import GarminNotFound


def _local_date(summary: Mapping[str, Any]) -> date | None:
    value = summary.get("startTimeLocal") or summary.get("startTimeGMT")
    return date.fromisoformat(str(value)[:10]) if value else None


class FakeGarminClient:
    """In-memory Garmin. Payloads are returned as deep copies, as a real client would."""

    def __init__(self) -> None:
        self.activities: dict[int, dict[str, Any]] = {}
        self.originals: dict[int, bytes] = {}
        self.sleep: dict[date, Any] = {}
        self.hrv: dict[date, Any] = {}
        self.stats: dict[date, Any] = {}
        self.steps: dict[date, Any] = {}
        self.personal_records: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self._failures: dict[str, list[BaseException | Callable[[], Any]]] = {}

    # Setting up ---------------------------------------------------------------------------

    def add_activity(self, summary: Mapping[str, Any], *, original: bytes | None = None) -> None:
        """Serve ``summary`` from :meth:`get_activities_by_date` and ``original`` (the zip or
        FIT bytes) from :meth:`download_original`; without ``original`` the download is 404."""
        activity_id = int(summary["activityId"])
        self.activities[activity_id] = dict(summary)
        if original is not None:
            self.originals[activity_id] = bytes(original)

    def set_wellness(
        self,
        day: date,
        *,
        sleep: Any = None,
        hrv: Any = None,
        stats: Any = None,
        steps: Any = None,
    ) -> None:
        """Set (or, with ``None``, leave unchanged) the payloads Garmin returns for ``day``."""
        for store, value in (
            (self.sleep, sleep),
            (self.hrv, hrv),
            (self.stats, stats),
            (self.steps, steps),
        ):
            if value is not None:
                store[day] = value

    def fail(self, method: str, *errors: BaseException | Callable[[], Any]) -> None:
        """Make the next calls of ``method`` raise ``errors`` (in order), or run a callable
        first (which may raise or return a value to serve instead)."""
        self._failures.setdefault(method, []).extend(errors)

    def _maybe_fail(self, method: str) -> tuple[bool, Any]:
        queue = self._failures.get(method)
        if not queue:
            return False, None
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return True, item()

    def _record(self, method: str, arg: Any = None) -> tuple[bool, Any]:
        self.calls.append((method, arg))
        return self._maybe_fail(method)

    def call_count(self, method: str | None = None) -> int:
        return sum(1 for name, _ in self.calls if method is None or name == method)

    # GarminClient -------------------------------------------------------------------------

    def get_activities_by_date(self, start: date, end: date) -> list[dict[str, Any]]:
        handled, value = self._record("get_activities_by_date", (start, end))
        if handled:
            return value
        items = [
            a
            for a in self.activities.values()
            if (d := _local_date(a)) is not None and start <= d <= end
        ]
        items.sort(key=lambda a: str(a.get("startTimeLocal") or ""), reverse=True)
        return copy.deepcopy(items)

    def download_original(self, activity_id: int) -> bytes:
        handled, value = self._record("download_original", int(activity_id))
        if handled:
            return value
        try:
            return self.originals[int(activity_id)]
        except KeyError:
            raise GarminNotFound(f"no original file for activity {activity_id}") from None

    def _day(self, method: str, store: Mapping[date, Any], day: date) -> Any:
        handled, value = self._record(method, day)
        if handled:
            return value
        return copy.deepcopy(store.get(day))

    def get_sleep_data(self, day: date) -> dict[str, Any] | None:
        return self._day("get_sleep_data", self.sleep, day)

    def get_hrv_data(self, day: date) -> dict[str, Any] | None:
        return self._day("get_hrv_data", self.hrv, day)

    def get_stats(self, day: date) -> dict[str, Any] | None:
        return self._day("get_stats", self.stats, day)

    def get_steps_data(self, day: date) -> list[dict[str, Any]] | None:
        return self._day("get_steps_data", self.steps, day)

    def get_personal_records(self) -> list[dict[str, Any]]:
        handled, value = self._record("get_personal_records")
        if handled:
            return value
        return copy.deepcopy(self.personal_records)

    # Convenience --------------------------------------------------------------------------

    def days_requested(self, method: str) -> list[date]:
        """The days passed to ``method`` (e.g. ``"get_sleep_data"``), in call order."""
        return [arg for name, arg in self.calls if name == method]
