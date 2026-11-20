"""Dates the way the agent writes them on the command line.

``YYYY-MM-DD``, ``today``, ``tomorrow``, ``yesterday``, an English weekday (``mon`` …
``sunday``: the next such day, today included) or ``+N`` days from today (``-N`` is parsed
too, but on the command line it looks like an option, so the help does not offer it). "Today" is
the athlete's local date (``COACH_TZ``), never the server's.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

DAY_HELP = (
    "YYYY-MM-DD, today, tomorrow, yesterday, a weekday such as fri (the next one), or +N days"
)

_OFFSET = re.compile(r"^[+-]\d{1,4}$")


def local_today(zone: ZoneInfo | None = None) -> date:
    """Today's date in ``zone`` (default: ``COACH_TZ``)."""
    if zone is None:
        from thresy.config import get_settings

        zone = get_settings().zone
    return datetime.now(zone).date()


def parse_day(text: str, today: date) -> date:
    """Parse one of the forms in :data:`DAY_HELP`; raises ``ValueError`` with a usable message."""
    value = text.strip().lower()
    if value == "today":
        return today
    if value == "tomorrow":
        return today + timedelta(days=1)
    if value == "yesterday":
        return today - timedelta(days=1)
    if value in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[value] - today.weekday()) % 7)
    if _OFFSET.match(value):
        return today + timedelta(days=int(value))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"not a date: {text!r} (use {DAY_HELP})") from None


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def parse_week(text: str | None, today: date) -> date:
    """The Monday of a week: ``this`` (default), ``next``, ``last``, or any day of the week."""
    value = (text or "this").strip().lower()
    if value in ("this", "current", "now"):
        return week_start(today)
    if value == "next":
        return week_start(today) + timedelta(days=7)
    if value in ("last", "previous", "prev"):
        return week_start(today) - timedelta(days=7)
    return week_start(parse_day(value, today))


def relative(day: date, today: date) -> str:
    """``today``, ``tomorrow``, ``in 5 days``, ``3 days ago`` …"""
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta > 0:
        return f"in {delta} days"
    return f"{-delta} days ago"


def short(day: date) -> str:
    """``Thu 2030-01-03``"""
    return day.strftime("%a %Y-%m-%d")
