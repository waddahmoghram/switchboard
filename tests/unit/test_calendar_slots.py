"""Unit tests for the free-time search."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from switchboard.connectors.calendar import slots

try:
    NEW_YORK = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - only on systems without IANA time zone data
    pytest.skip("IANA time zone data is not installed", allow_module_level=True)

WEEKDAYS = {0, 1, 2, 3, 4}


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_busy_time_is_merged_and_subtracted_inside_working_hours() -> None:
    busy = [
        (_utc(2026, 11, 9, 10, 0), _utc(2026, 11, 9, 11, 0)),
        (_utc(2026, 11, 9, 10, 30), _utc(2026, 11, 9, 12, 0)),
        (_utc(2026, 11, 9, 16, 30), _utc(2026, 11, 9, 18, 0)),
        (_utc(2026, 11, 10, 8, 0), _utc(2026, 11, 10, 9, 30)),
    ]
    found = slots.free_slots(
        busy,
        _utc(2026, 11, 9),
        _utc(2026, 11, 11),
        zone=UTC,
        day_start=time(9),
        day_end=time(17),
        weekdays=WEEKDAYS,
        minimum=timedelta(minutes=60),
    )
    assert found == [
        (_utc(2026, 11, 9, 9, 0), _utc(2026, 11, 9, 10, 0)),
        (_utc(2026, 11, 9, 12, 0), _utc(2026, 11, 9, 16, 30)),
        (_utc(2026, 11, 10, 9, 30), _utc(2026, 11, 10, 17, 0)),
    ]


def test_windows_shorter_than_the_minimum_are_dropped() -> None:
    busy = [(_utc(2026, 11, 9, 10, 0), _utc(2026, 11, 9, 12, 0))]
    found = slots.free_slots(
        busy,
        _utc(2026, 11, 9),
        _utc(2026, 11, 10),
        zone=UTC,
        day_start=time(9),
        day_end=time(17),
        weekdays=WEEKDAYS,
        minimum=timedelta(minutes=120),
    )
    assert found == [(_utc(2026, 11, 9, 12, 0), _utc(2026, 11, 9, 17, 0))]


def test_working_hours_follow_the_wall_clock_across_dst_and_skip_weekends() -> None:
    found = slots.free_slots(
        [],
        datetime(2026, 10, 30, tzinfo=NEW_YORK),
        datetime(2026, 11, 3, tzinfo=NEW_YORK),
        zone=NEW_YORK,
        day_start=time(9),
        day_end=time(17),
        weekdays=WEEKDAYS,
        minimum=timedelta(minutes=30),
    )
    assert found == [
        (_utc(2026, 10, 30, 13, 0), _utc(2026, 10, 30, 21, 0)),
        (_utc(2026, 11, 2, 14, 0), _utc(2026, 11, 2, 22, 0)),
    ]


def test_limit_caps_the_number_of_slots() -> None:
    found = slots.free_slots(
        [],
        _utc(2026, 11, 9),
        _utc(2026, 11, 14),
        zone=UTC,
        day_start=time(9),
        day_end=time(17),
        weekdays=WEEKDAYS,
        minimum=timedelta(minutes=30),
        limit=2,
    )
    assert [start.day for start, _ in found] == [9, 10]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("09:00-17:00", (time(9), time(17))), ("22:30-24:00", (time(22, 30), None))],
)
def test_parse_working_hours(text: str, expected: tuple[time, time | None]) -> None:
    assert slots.parse_working_hours(text) == expected


@pytest.mark.parametrize("text", ["17:00-09:00", "9-5", "24:00-24:00", "09:00-09:00"])
def test_parse_working_hours_rejects_bad_ranges(text: str) -> None:
    with pytest.raises(ValueError, match="working_hours"):
        slots.parse_working_hours(text)
