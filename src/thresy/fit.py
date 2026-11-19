"""FIT file parsing: a file in, per-second records and laps out. No database access.

Field names and units follow the ``activity_records`` and ``activity_laps`` tables of the
database contract, so a :class:`Record` or :class:`Lap` can be written as is
(see :func:`thresy.store.replace_activity_records`).

Behaviour matches the old bot's parser:

* positions are converted from semicircles to degrees;
* ``enhanced_speed`` / ``enhanced_altitude`` are preferred over ``speed`` / ``altitude``;
* cadence is converted to steps per minute: ``(cadence + fractional_cadence) * 2``;
* respiration rate comes from record field number 108, which fitparse 1.2 does not know
  (``unknown_108``), scaled by 1/100;
* ``elapsed_s`` counts from the first record's timestamp;
* lap cadence is ``(avg_running_cadence + avg_fractional_cadence) * 2``.

Differences from the old parser (all deliberate):

* For cycling activities (session sport ``cycling`` or ``e_biking``) record cadence is
  revolutions, not steps, so ``cadence_spm`` is left empty instead of doubling RPM.
* Records with a timestamp that already occurred are dropped (first one wins), because
  ``(activity_id, ts)`` is the primary key. Garmin files rarely contain such duplicates.
* Missing lap ``intensity`` / ``trigger`` values are ``None`` rather than the string
  ``"None"``, and intensity codes newer than fitparse's profile (4, 5, 6) are translated to
  their FIT SDK names (``recovery``, ``interval``, ``other``).
"""

from __future__ import annotations

import io
import warnings
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

SEMICIRCLE_TO_DEG = 180 / 2**31

# FIT SDK "intensity" enum. fitparse 1.2 only knows 0-3 and returns the raw int for the rest.
FIT_INTENSITY = {
    0: "active",
    1: "rest",
    2: "warmup",
    3: "cooldown",
    4: "recovery",
    5: "interval",
    6: "other",
}

# Sports whose record "cadence" is crank revolutions, not steps.
RPM_CADENCE_SPORTS = frozenset({"cycling", "e_biking"})

RESP_RATE_FIELD_NUM = 108  # enhanced_respiration_rate, breaths/min * 100


class FitError(ValueError):
    """The file could not be read as a FIT activity file."""


@dataclass(frozen=True, slots=True)
class Record:
    """One sample, matching a row of ``activity_records`` (without ``activity_id``)."""

    ts: datetime  # UTC, timezone-aware
    elapsed_s: float  # seconds since the first record
    lat: float | None  # degrees
    lon: float | None  # degrees
    alt_m: float | None
    dist_m: float | None  # cumulative distance
    speed_mps: float | None
    hr: int | None  # bpm
    cadence_spm: float | None  # steps per minute, both feet
    power_w: float | None
    resp_rate: float | None  # breaths per minute
    temp_c: float | None


@dataclass(frozen=True, slots=True)
class Lap:
    """One lap, matching a row of ``activity_laps`` (without ``activity_id``)."""

    lap_no: int  # 1-based, in file order
    start_utc: datetime | None
    duration_s: float | None  # timer time
    elapsed_s: float | None
    dist_m: float | None
    avg_speed_mps: float | None
    max_speed_mps: float | None
    avg_hr: float | None
    max_hr: float | None
    avg_cadence_spm: float | None
    avg_power_w: float | None
    ascent_m: float | None
    descent_m: float | None
    avg_temp_c: float | None
    intensity: str | None
    trigger: str | None
    wkt_step: int | None


RECORD_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(Record))
LAP_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(Lap))


@dataclass(frozen=True)
class FitData:
    """Everything :func:`parse_fit` extracts from one file."""

    sport: str | None  # FIT session sport, e.g. "running"
    sub_sport: str | None  # e.g. "trail", "treadmill", "generic"
    records: list[Record]
    laps: list[Lap]


# Small pure helpers (unit-testable without a file) -------------------------------------------


def semicircles_to_deg(value: float | None) -> float | None:
    """Convert a FIT semicircle position to degrees."""
    return None if value is None else value * SEMICIRCLE_TO_DEG


def as_utc(value: datetime | None) -> datetime | None:
    """fitparse returns naive datetimes that are UTC; make them timezone-aware."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def steps_per_minute(cadence: float | None, fractional: float | None = None) -> float | None:
    """FIT running cadence (strides/min of one foot) plus its fraction, as steps/min."""
    if cadence is None:
        return None
    return (cadence + (fractional or 0)) * 2


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = values.get(key)
        if value is not None:
            return value
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def intensity_name(value: Any) -> str | None:
    """Lap intensity as text; integers outside fitparse's profile get their FIT SDK name."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return FIT_INTENSITY.get(value, str(value))
    return str(value)


def record_from_values(
    values: Mapping[str, Any], first_ts: datetime, *, cadence_is_steps: bool = True
) -> Record:
    """Build a :class:`Record` from one record message's field values.

    ``values`` maps fitparse field names to values; the key ``"_resp"`` carries the already
    scaled respiration rate from field 108 when present. ``first_ts`` is the (aware) timestamp
    of the activity's first record.
    """
    ts = as_utc(values["timestamp"])
    assert ts is not None
    cadence = (
        steps_per_minute(_num(values.get("cadence")), _num(values.get("fractional_cadence")))
        if cadence_is_steps
        else None
    )
    if "_resp" in values:
        resp = values["_resp"]
    else:
        resp = _first(values, "enhanced_respiration_rate", "respiration_rate")
    hr = values.get("heart_rate")
    return Record(
        ts=ts,
        elapsed_s=(ts - first_ts).total_seconds(),
        lat=semicircles_to_deg(_num(values.get("position_lat"))),
        lon=semicircles_to_deg(_num(values.get("position_long"))),
        alt_m=_num(_first(values, "enhanced_altitude", "altitude")),
        dist_m=_num(values.get("distance")),
        speed_mps=_num(_first(values, "enhanced_speed", "speed")),
        hr=int(hr) if isinstance(hr, int | float) and not isinstance(hr, bool) else None,
        cadence_spm=cadence,
        power_w=_num(values.get("power")),
        resp_rate=_num(resp),
        temp_c=_num(values.get("temperature")),
    )


def lap_from_values(lap_no: int, values: Mapping[str, Any]) -> Lap:
    """Build a :class:`Lap` from one lap message's field values (``message.get_values()``)."""
    running_cadence = _num(values.get("avg_running_cadence"))
    wkt_step = values.get("wkt_step_index")
    trigger = values.get("lap_trigger")
    return Lap(
        lap_no=lap_no,
        start_utc=as_utc(values.get("start_time")),
        duration_s=_num(values.get("total_timer_time")),
        elapsed_s=_num(values.get("total_elapsed_time")),
        dist_m=_num(values.get("total_distance")),
        avg_speed_mps=_num(_first(values, "enhanced_avg_speed", "avg_speed")),
        max_speed_mps=_num(_first(values, "enhanced_max_speed", "max_speed")),
        avg_hr=_num(values.get("avg_heart_rate")),
        max_hr=_num(values.get("max_heart_rate")),
        avg_cadence_spm=(
            steps_per_minute(running_cadence, _num(values.get("avg_fractional_cadence")))
            if running_cadence  # the old parser treated 0 as "no cadence"
            else None
        ),
        avg_power_w=_num(values.get("avg_power")),
        ascent_m=_num(values.get("total_ascent")),
        descent_m=_num(values.get("total_descent")),
        avg_temp_c=_num(values.get("avg_temperature")),
        intensity=intensity_name(values.get("intensity")),
        trigger=None if trigger is None else str(trigger),
        wkt_step=(
            int(wkt_step)
            if isinstance(wkt_step, int | float) and not isinstance(wkt_step, bool)
            else None
        ),
    )


# File parsing --------------------------------------------------------------------------------


def _record_values(message: Any) -> dict[str, Any]:
    """Field values of a fitparse record message, plus ``_resp`` from field 108."""
    values: dict[str, Any] = {}
    for fd in message.fields:
        values[fd.name] = fd.value
        if fd.def_num == RESP_RATE_FIELD_NUM and fd.name.startswith("unknown"):
            values["_resp"] = fd.value / 100 if fd.value is not None else None
    return values


def parse_fit(source: str | Path | bytes | BinaryIO) -> FitData:
    """Parse a FIT activity file (a path, the file's bytes, or a binary file object).

    Raises :class:`FitError` when the file cannot be parsed. A valid file without records
    returns empty lists.
    """
    import fitparse  # imported lazily: keeps `coach --help` fast

    src: Any = str(source) if isinstance(source, Path) else source
    try:
        with warnings.catch_warnings():
            # fitparse 1.2 calls datetime.utcfromtimestamp(), deprecated since Python 3.12.
            warnings.filterwarnings("ignore", category=DeprecationWarning, module="fitparse")
            fit = fitparse.FitFile(src)
            fit.parse()  # decodes every message now; get_messages() below reads the cache
    except (fitparse.FitParseError, OSError, ValueError) as exc:
        raise FitError(f"cannot parse FIT file: {exc}") from exc

    sport = sub_sport = None
    for message in fit.get_messages("session"):
        values = message.get_values()
        sport = _str_or_none(values.get("sport"))
        sub_sport = _str_or_none(values.get("sub_sport"))
        break
    cadence_is_steps = sport not in RPM_CADENCE_SPORTS

    records: list[Record] = []
    seen: set[datetime] = set()
    first_ts: datetime | None = None
    for message in fit.get_messages("record"):
        values = _record_values(message)
        if values.get("timestamp") is None:
            continue
        ts = as_utc(values["timestamp"])
        assert ts is not None
        if ts in seen:
            continue
        seen.add(ts)
        if first_ts is None:
            first_ts = ts
        records.append(record_from_values(values, first_ts, cadence_is_steps=cadence_is_steps))

    laps = [
        lap_from_values(n, message.get_values())
        for n, message in enumerate(fit.get_messages("lap"), start=1)
    ]
    return FitData(sport=sport, sub_sport=sub_sport, records=records, laps=laps)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def fit_bytes_from_zip(data: bytes) -> bytes:
    """Return the first ``.fit`` member of a zip archive (Garmin's "original" download format)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".fit"):
                return archive.read(name)
    raise FitError("zip archive contains no .fit file")
