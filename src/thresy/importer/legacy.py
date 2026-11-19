"""Import the old bot's export into Postgres, through :mod:`thresy.store` only.

What is read, relative to the export directory:

* **Activities**: ``overviews/year-*.json``, ``overviews/ytd-*.json``,
  ``overviews/activities-*.json`` (lists of Garmin activity summaries) and ``activities/*.json``
  (one summary each). The union is imported; when an activity appears in several files the
  later file wins, in the order listed (the old store's order).
* **Per-second data**: ``tracks/<activity id>.fit``. Each file is parsed with
  :func:`thresy.fit.parse_fit`, copied to ``fit/<YYYY>/<id>.fit`` under ``COACH_DATA_DIR``
  (``YYYY`` = year of the local start) and its records and laps are stored. CSV and GPX files
  in ``tracks/`` are ignored.
* **Wellness**: ``daily/20*.json`` reports, each holding sleep, HRV, stats and step payloads
  for two days. Every payload is keyed by its own calendar date (``dailySleepDTO.calendarDate``,
  ``hrvSummary.calendarDate``, ``calendarDate``), and the report's ``synced_at`` becomes the
  night's ``sleep_synced_at``. Reports are applied in file-name order.
* **Personal records**: ``overviews/personal-records.json`` (Garmin's list) and
  ``overviews/pr-hm-marathon.json`` (the old bot's computed half-marathon and marathon bests).

Rules:

* No events are raised (the store functions never insert any).
* Idempotent and resumable. Activities whose stored payload already matches are skipped;
  a FIT file whose copy and ``fit_path`` are in place is skipped; processed daily reports are
  remembered in ``sync_state`` (key :data:`WELLNESS_STATE_KEY`, file name -> SHA-256), and a
  run continues with the first report that is new or changed (and every report after it, so
  that later reports still win).
* Data written by another source is never overwritten: activities whose ``source`` is not
  :data:`SOURCE`, wellness days the importer did not create, FIT copies that differ from the
  legacy file, and personal records whose key already exists are left alone and counted as
  ``skipped_*``. Run the import before the first Garmin sync to get everything.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import logging
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from thresy import store
from thresy.fit import FitError, parse_fit

log = logging.getLogger(__name__)

SOURCE = "legacy"
PARTS = ("activities", "records", "wellness", "prs")
WELLNESS_STATE_KEY = "legacy_import.wellness"

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


# Reading the export ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyExport:
    """The old bot's export directory."""

    root: Path

    def check(self) -> None:
        """Raise ``FileNotFoundError`` unless ``root`` looks like an export."""
        if not self.root.is_dir():
            raise FileNotFoundError(f"legacy directory not found: {self.root}")
        if not any((self.root / d).is_dir() for d in ("overviews", "activities", "tracks")):
            raise FileNotFoundError(
                f"{self.root} has none of overviews/, activities/, tracks/ (not a legacy export?)"
            )

    def activity_files(self) -> list[Path]:
        """Summary files in precedence order (later files win)."""
        ov = self.root / "overviews"
        files = [
            *sorted(ov.glob("year-*.json")),
            *sorted(ov.glob("ytd-*.json")),
            *sorted(ov.glob("activities-*.json")),
        ]
        return [*files, *sorted((self.root / "activities").glob("*.json"))]

    def activity_payloads(self) -> dict[int, dict[str, Any]]:
        """Every activity summary by id; later files win."""
        payloads: dict[int, dict[str, Any]] = {}
        for path in self.activity_files():
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("activityId") in (None, ""):
                    continue
                payloads[int(item["activityId"])] = item
        return payloads

    def fit_file(self, activity_id: int) -> Path | None:
        path = self.root / "tracks" / f"{activity_id}.fit"
        return path if path.is_file() else None

    def daily_reports(self) -> list[Path]:
        return sorted((self.root / "daily").glob("20*.json"))

    def garmin_personal_records(self) -> list[store.PersonalRecord]:
        path = self.root / "overviews" / "personal-records.json"
        if not path.is_file():
            return []
        return store.garmin_personal_records(json.loads(path.read_text(encoding="utf-8")))

    def computed_personal_records(self) -> list[store.PersonalRecord]:
        """The old bot's computed records (``pr-hm-marathon.json``), as its store kept them."""
        path = self.root / "overviews" / "pr-hm-marathon.json"
        if not path.is_file():
            return []
        out = []
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            if not isinstance(value, dict) or not value.get("id"):
                continue
            out.append(
                store.PersonalRecord(
                    key=f"computed_{key}",
                    label=key,
                    value=_hms_seconds(value.get("time")) or _float(value.get("moving_s")),
                    activity_id=int(value["id"]),
                    activity_name=value.get("name"),
                    date=date.fromisoformat(value["date"][:10]) if value.get("date") else None,
                    source="computed",
                )
            )
        return out


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _hms_seconds(text: Any) -> float | None:
    """``"1:19:59"`` -> 4799.0; anything else -> None."""
    if not isinstance(text, str) or text.count(":") != 2:
        return None
    try:
        h, m, s = (int(part) for part in text.split(":"))
    except ValueError:
        return None
    return float(h * 3600 + m * 60 + s)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Selection ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """Which activities to import (all by default)."""

    ids: frozenset[int] | None = None
    since: date | None = None
    until: date | None = None
    limit: int | None = None
    """The most recent ``limit`` activities after the other filters."""


def select_activities(
    payloads: Mapping[int, Mapping[str, Any]], selection: Selection
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    """Filter and order (newest first) the activities to import.

    Returns ``(selected, invalid)`` where ``invalid`` counts payloads without a start time.
    """
    rows = []
    invalid = 0
    for activity_id, payload in payloads.items():
        try:
            row = store.activity_row(payload)
        except ValueError:
            invalid += 1
            continue
        if selection.ids is not None and activity_id not in selection.ids:
            continue
        if selection.since and row["local_date"] < selection.since:
            continue
        if selection.until and row["local_date"] > selection.until:
            continue
        start = row["start_local"] or datetime.combine(row["local_date"], datetime.min.time())
        rows.append((start, activity_id, dict(payload)))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    if selection.limit is not None:
        rows = rows[: selection.limit]
    return [(activity_id, payload) for _, activity_id, payload in rows], invalid


# Summary --------------------------------------------------------------------------------------


@dataclass
class ImportSummary:
    activities: Counter[str] = field(default_factory=Counter)
    records: Counter[str] = field(default_factory=Counter)
    wellness: Counter[str] = field(default_factory=Counter)
    prs: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "activities": dict(self.activities),
            "records": dict(self.records),
            "wellness": dict(self.wellness),
            "prs": dict(self.prs),
            "errors": self.errors,
            "duration_s": round(self.duration_s, 1),
        }

    def text(self) -> str:
        def line(name: str, counts: Counter[str]) -> str:
            if not counts:
                return f"{name:<11} -"
            return f"{name:<11} " + ", ".join(f"{k} {v}" for k, v in counts.items())

        lines = [
            line("activities", self.activities),
            line("records", self.records),
            line("wellness", self.wellness),
            line("prs", self.prs),
        ]
        if self.errors:
            lines.append(f"errors ({len(self.errors)}):")
            lines.extend(f"  {e}" for e in self.errors[:20])
            if len(self.errors) > 20:
                lines.append(f"  ... and {len(self.errors) - 20} more")
        lines.append(f"took {self.duration_s:.1f} s")
        return "\n".join(lines)


# Parts ----------------------------------------------------------------------------------------


def import_activities(
    conn: store.Conn,
    selected: list[tuple[int, dict[str, Any]]],
    summary: ImportSummary,
    progress: Progress = _quiet,
) -> None:
    counts = summary.activities
    ids = [activity_id for activity_id, _ in selected]
    existing = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, source, raw FROM activities WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    }
    for n, (activity_id, payload) in enumerate(selected, start=1):
        old = existing.get(activity_id)
        if old is not None and old["source"] != SOURCE:
            counts["skipped_other_source"] += 1
        elif old is not None and old["raw"] == payload:
            counts["unchanged"] += 1
        else:
            try:
                is_new = store.upsert_activity(conn, payload, source=SOURCE)
            except ValueError as exc:
                counts["failed"] += 1
                summary.errors.append(f"activity {activity_id}: {exc}")
                continue
            counts["new" if is_new else "updated"] += 1
        if n % 250 == 0:
            progress(f"activities: {n}/{len(selected)}")
    counts["selected"] = len(selected)


def fit_relpath(activity_id: int, year: int) -> str:
    """Path of an original FIT file relative to ``COACH_DATA_DIR`` (contract: Files)."""
    return f"fit/{year:04d}/{activity_id}.fit"


def _copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(src, tmp)
    tmp.replace(dest)


def import_records(
    conn: store.Conn,
    export: LegacyExport,
    activity_ids: Iterable[int],
    data_dir: Path,
    summary: ImportSummary,
    progress: Progress = _quiet,
) -> None:
    counts = summary.records
    with_fit = [(a, p) for a in activity_ids if (p := export.fit_file(a)) is not None]
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, local_date, start_local, fit_path FROM activities WHERE id = ANY(%s)",
            ([a for a, _ in with_fit],),
        ).fetchall()
    }
    started = time.monotonic()
    for n, (activity_id, src) in enumerate(with_fit, start=1):
        row = rows.get(activity_id)
        if row is None:
            counts["missing_activity"] += 1
        else:
            year = (row["start_local"] or row["local_date"]).year
            rel = fit_relpath(activity_id, year)
            _import_one_fit(conn, activity_id, src, data_dir, rel, row["fit_path"], summary)
        if n % 10 == 0 or n == len(with_fit):
            rate = n / max(time.monotonic() - started, 1e-6)
            progress(f"records: {n}/{len(with_fit)} files ({rate:.1f}/s)")
    counts["files"] = len(with_fit)


def _import_one_fit(
    conn: store.Conn,
    activity_id: int,
    src: Path,
    data_dir: Path,
    rel: str,
    stored_rel: str | None,
    summary: ImportSummary,
) -> None:
    counts = summary.records
    dest = data_dir / rel
    same_file = dest.is_file() and filecmp.cmp(src, dest, shallow=False)
    if stored_rel == rel and same_file:
        counts["unchanged"] += 1
        return
    if dest.is_file() and not same_file:
        # Someone else (the Garmin sync) stored a different original; keep theirs.
        counts["skipped_other_file"] += 1
        return
    if stored_rel not in (None, rel):
        counts["skipped_other_file"] += 1
        return
    try:
        data = parse_fit(src)
    except FitError as exc:
        counts["failed"] += 1
        summary.errors.append(f"{src.name}: {exc}")
        return
    if not same_file:
        _copy_atomic(src, dest)
    n = store.replace_activity_records(conn, activity_id, data.records, data.laps, fit_path=rel)
    counts["imported"] += 1
    counts["records"] += n
    counts["laps"] += len(data.laps)


# Wellness -------------------------------------------------------------------------------------

_DATE_KEYS = {
    "sleep": ("dailySleepDTO", "calendarDate"),
    "hrv": ("hrvSummary", "calendarDate"),
    "stats": (None, "calendarDate"),
}


def _payload_date(kind: str, payload: Any) -> date | None:
    if kind not in _DATE_KEYS or not isinstance(payload, Mapping):
        return None
    outer, key = _DATE_KEYS[kind]
    obj = payload.get(outer) if outer else payload
    value = obj.get(key) if isinstance(obj, Mapping) else None
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _usable(kind: str, payload: Any) -> bool:
    if kind == "steps":
        return isinstance(payload, list) and bool(payload)
    return store.is_ok_payload(payload) and bool(payload)


def report_payloads(report: Mapping[str, Any]) -> dict[date, dict[str, Any]]:
    """The payloads of one daily report, grouped by each payload's own calendar date.

    Step interval lists carry no calendar date and stay with the day the report names. When a
    report holds two payloads of one kind for the same day, real sleep beats a placeholder and
    otherwise the later one (``today``) wins.
    """
    wellness = report.get("wellness") or {}
    out: dict[date, dict[str, Any]] = defaultdict(dict)
    for tag in ("yesterday", "today"):
        try:
            tag_day = date.fromisoformat(str(wellness.get(tag))[:10])
        except ValueError:
            tag_day = None
        for kind in ("sleep", "hrv", "stats", "steps"):
            payload = wellness.get(f"{kind}_{tag}")
            if not _usable(kind, payload):
                continue
            day = _payload_date(kind, payload) or tag_day
            if day is None:
                continue
            prev = out[day].get(kind)
            if (
                kind == "sleep"
                and prev is not None
                and store.is_real_sleep(prev)
                and not store.is_real_sleep(payload)
            ):
                continue
            out[day][kind] = payload
    return dict(out)


def _report_synced_at(report: Mapping[str, Any]) -> datetime | None:
    value = report.get("synced_at")
    if not value:
        return None
    try:
        return store.parse_garmin_time(value, utc=True)
    except ValueError:
        return None


def import_wellness(
    conn: store.Conn,
    export: LegacyExport,
    summary: ImportSummary,
    progress: Progress = _quiet,
) -> None:
    counts = summary.wellness
    reports = export.daily_reports()
    hashes = {p.name: _sha256(p) for p in reports}
    state = store.get_sync_state(conn, WELLNESS_STATE_KEY, {}) or {}
    done: dict[str, str] = dict(state.get("files") or {})
    own_days = set(state.get("days") or [])
    first = next(
        (i for i, p in enumerate(reports) if done.get(p.name) != hashes[p.name]), len(reports)
    )
    counts["reports"] = len(reports)
    counts["reports_unchanged"] = first
    existing = {
        r["wake_date"].isoformat() for r in conn.execute("SELECT wake_date FROM wellness_daily")
    }
    nights: set[str] = set()
    for n, path in enumerate(reports[first:], start=first + 1):
        report = json.loads(path.read_text(encoding="utf-8"))
        synced_at = _report_synced_at(report)
        with conn.transaction():
            for day, payloads in sorted(report_payloads(report).items()):
                key = day.isoformat()
                if key in existing and key not in own_days:
                    counts["skipped_other_source"] += 1
                    continue
                try:
                    result = store.upsert_wellness(conn, day, synced_at=synced_at, **payloads)
                except ValueError as exc:
                    counts["failed"] += 1
                    summary.errors.append(f"{path.name} {key}: {exc}")
                    continue
                counts["upserts"] += 1
                if result.sleep_synced_at is not None:
                    nights.add(key)
                own_days.add(key)
                existing.add(key)
            done[path.name] = hashes[path.name]
            store.set_sync_state(
                conn, WELLNESS_STATE_KEY, {"files": done, "days": sorted(own_days)}
            )
        counts["reports_imported"] += 1
        if n % 10 == 0 or n == len(reports):
            progress(f"wellness: {n}/{len(reports)} reports")
    counts["days"] = len(own_days)
    if nights:
        counts["nights_with_sleep"] = len(nights)


# Personal records -----------------------------------------------------------------------------


def import_personal_records(conn: store.Conn, export: LegacyExport, summary: ImportSummary) -> None:
    counts = summary.prs
    records = export.garmin_personal_records() + export.computed_personal_records()
    existing = {r["key"] for r in conn.execute("SELECT key FROM personal_records")}
    new = [r for r in records if r.key not in existing]
    store.upsert_personal_records(conn, new)
    counts["found"] = len(records)
    counts["new"] = len(new)
    counts["existing"] = len(records) - len(new)


# Everything -----------------------------------------------------------------------------------


def run_import(
    conn: store.Conn,
    legacy_dir: Path,
    data_dir: Path,
    *,
    parts: Iterable[str] = PARTS,
    selection: Selection | None = None,
    progress: Progress = _quiet,
) -> ImportSummary:
    """Import the selected parts of the export at ``legacy_dir``; see the module docstring.

    ``selection`` restricts activities (and therefore per-second data); wellness and personal
    records are always imported in full.
    """
    parts = set(parts)
    unknown = parts - set(PARTS)
    if unknown:
        raise ValueError(f"unknown part(s): {', '.join(sorted(unknown))}")
    export = LegacyExport(Path(legacy_dir))
    export.check()
    selection = selection or Selection()
    summary = ImportSummary()
    started = time.monotonic()

    selected: list[tuple[int, dict[str, Any]]] = []
    if parts & {"activities", "records"}:
        payloads = export.activity_payloads()
        selected, invalid = select_activities(payloads, selection)
        progress(f"activities: {len(payloads)} in the export, {len(selected)} selected")
        if invalid:
            summary.activities["invalid"] = invalid
    if "activities" in parts:
        import_activities(conn, selected, summary, progress)
    if "records" in parts:
        import_records(conn, export, [a for a, _ in selected], Path(data_dir), summary, progress)
    if "wellness" in parts:
        import_wellness(conn, export, summary, progress)
    if "prs" in parts:
        import_personal_records(conn, export, summary)
    summary.duration_s = time.monotonic() - started
    return summary
