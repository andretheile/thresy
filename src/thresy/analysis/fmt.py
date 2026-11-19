"""Text formatting for the command output: paces, durations and aligned tables."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def pace(s_per_km: float | None) -> str:
    """``275.4`` -> ``"4:35"`` (per km)."""
    if _missing(s_per_km) or s_per_km <= 0 or s_per_km > 3600:  # type: ignore[operator]
        return "-"
    total = round(float(s_per_km))  # type: ignore[arg-type]
    return f"{total // 60}:{total % 60:02d}"


def duration(seconds: float | None) -> str:
    """``75`` -> ``"1:15"``, ``3725`` -> ``"1:02:05"``."""
    if _missing(seconds):
        return "-"
    total = round(float(seconds))  # type: ignore[arg-type]
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"


def signed_duration(seconds: float | None) -> str:
    if _missing(seconds):
        return "-"
    return ("+" if seconds >= 0 else "") + duration(seconds)  # type: ignore[operator]


def num(value: float | None, digits: int = 0, unit: str = "") -> str:
    if _missing(value):
        return "-"
    text = f"{value:.{digits}f}"
    return f"{text} {unit}".rstrip() if unit else text


def km(metres: float | None, digits: int = 2) -> str:
    return "-" if _missing(metres) else f"{metres / 1000:.{digits}f}"  # type: ignore[operator]


def when(value: datetime | date | None, *, seconds: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
    return value.isoformat()


def clock(value: datetime | None) -> str:
    return "-" if value is None else value.strftime("%H:%M")


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, str]]) -> str:
    """An aligned text table. ``columns`` are ``(key, header)`` pairs; values are printed with
    ``str`` (format them first). Numbers-looking columns are right-aligned."""
    if not rows:
        return "(none)"
    cells = [[str(row.get(key, "")) for key, _ in columns] for row in rows]
    headers = [header for _, header in columns]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]

    def numeric(i: int) -> bool:
        values = [r[i] for r in cells if r[i] not in ("", "-")]
        return bool(values) and all(
            v.lstrip("+-").replace(":", "").replace(".", "").replace("%", "").isdigit()
            for v in values
        )

    right = [numeric(i) for i in range(len(headers))]

    def line(values: Sequence[str]) -> str:
        parts = [
            v.rjust(widths[i]) if right[i] else v.ljust(widths[i]) for i, v in enumerate(values)
        ]
        return "  ".join(parts).rstrip()

    return "\n".join([line(headers), *(line(r) for r in cells)])


def rounded(value: float | None, digits: int = 1) -> float | None:
    """Round for JSON output, keeping ``None`` (and turning NaN into ``None``)."""
    if _missing(value):
        return None
    return round(float(value), digits)  # type: ignore[arg-type]
