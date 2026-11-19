"""Dates as the agent writes them."""

from __future__ import annotations

from datetime import date

import pytest

from thresy.tools import dates

WEDNESDAY = date(2030, 1, 2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", WEDNESDAY),
        ("Tomorrow", date(2030, 1, 3)),
        ("yesterday", date(2030, 1, 1)),
        ("wed", WEDNESDAY),
        ("thu", date(2030, 1, 3)),
        ("monday", date(2030, 1, 7)),
        ("+10", date(2030, 1, 12)),
        ("-2", date(2029, 12, 31)),
        ("2030-02-01", date(2030, 2, 1)),
    ],
)
def test_parse_day(text: str, expected: date) -> None:
    assert dates.parse_day(text, WEDNESDAY) == expected


def test_parse_day_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="not a date: 'soon'"):
        dates.parse_day("soon", WEDNESDAY)


def test_weeks() -> None:
    assert dates.parse_week(None, WEDNESDAY) == date(2029, 12, 31)
    assert dates.parse_week("next", WEDNESDAY) == date(2030, 1, 7)
    assert dates.parse_week("last", WEDNESDAY) == date(2029, 12, 24)
    assert dates.parse_week("2030-01-06", WEDNESDAY) == date(2029, 12, 31)


def test_relative() -> None:
    assert dates.relative(WEDNESDAY, WEDNESDAY) == "today"
    assert dates.relative(date(2030, 1, 5), WEDNESDAY) == "in 3 days"
    assert dates.relative(date(2029, 12, 30), WEDNESDAY) == "3 days ago"
