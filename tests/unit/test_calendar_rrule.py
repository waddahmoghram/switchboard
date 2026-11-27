"""Unit tests for recurrence expansion: BY* rules, COUNT/UNTIL, EXDATE, overrides, DST, and limits."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from switchboard.connectors.calendar import ics, rrule

try:
    NEW_YORK = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - only on systems without IANA time zone data
    pytest.skip("IANA time zone data is not installed", allow_module_level=True)


def _event(rule: str, start: datetime, minutes: int = 30, **fields: Any) -> ics.Event:
    return ics.Event(
        uid=fields.pop("uid", "series"),
        summary=fields.pop("summary", "Series"),
        start=start,
        end=start + timedelta(minutes=minutes),
        rrule=rule,
        **fields,
    )


def _starts(events: Sequence[ics.Event], low: datetime, high: datetime) -> list[datetime]:
    found, warnings = rrule.expand(events, low, high)
    assert warnings == []
    return [item.start for item in found]


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_weekly_byday_lists_each_weekday() -> None:
    start = _utc(2026, 11, 2, 9, 0)  # a Monday
    found = _starts([_event("FREQ=WEEKLY;BYDAY=MO,WE,FR", start)], _utc(2026, 11, 1), _utc(2026, 11, 15))
    assert [moment.day for moment in found] == [2, 4, 6, 9, 11, 13]


def test_weekly_interval_with_wkst_matches_the_rfc_5545_example() -> None:
    start = datetime(1997, 8, 5, 9, 0, tzinfo=NEW_YORK)
    low, high = datetime(1997, 8, 1, tzinfo=NEW_YORK), datetime(1997, 10, 1, tzinfo=NEW_YORK)
    monday = _starts([_event("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", start)], low, high)
    sunday = _starts([_event("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU", start)], low, high)
    assert [(moment.month, moment.day) for moment in monday] == [(8, 5), (8, 10), (8, 19), (8, 24)]
    assert [(moment.month, moment.day) for moment in sunday] == [(8, 5), (8, 17), (8, 19), (8, 31)]


def test_monthly_last_friday() -> None:
    start = _utc(2026, 1, 30, 17, 0)
    found = _starts([_event("FREQ=MONTHLY;BYDAY=-1FR", start)], _utc(2026, 1, 1), _utc(2026, 6, 1))
    assert [(moment.month, moment.day) for moment in found] == [(1, 30), (2, 27), (3, 27), (4, 24), (5, 29)]


def test_monthly_last_day_of_the_month() -> None:
    start = _utc(2026, 1, 31, 12, 0)
    found = _starts([_event("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=4", start)], _utc(2026, 1, 1), _utc(2027, 1, 1))
    assert [(moment.month, moment.day) for moment in found] == [(1, 31), (2, 28), (3, 31), (4, 30)]


def test_yearly_fourth_thursday_of_november() -> None:
    start = _utc(2026, 11, 26, 17, 0)
    found = _starts([_event("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH", start)], _utc(2026, 1, 1), _utc(2029, 1, 1))
    assert [moment.date().isoformat() for moment in found] == ["2026-11-26", "2027-11-25", "2028-11-23"]


def test_count_and_until_bound_the_series() -> None:
    start = _utc(2026, 11, 2, 9, 0)
    low, high = _utc(2026, 11, 1), _utc(2026, 12, 1)
    assert len(_starts([_event("FREQ=DAILY;COUNT=3", start)], low, high)) == 3
    until = _starts([_event("FREQ=DAILY;UNTIL=20261104T090000Z", start)], low, high)
    assert [moment.day for moment in until] == [2, 3, 4]
    every_other = _starts([_event("FREQ=DAILY;INTERVAL=2;UNTIL=20261108", start)], low, high)
    assert [moment.day for moment in every_other] == [2, 4, 6, 8]


def test_exdate_removes_an_instance_that_still_counts_toward_count() -> None:
    start = _utc(2026, 11, 2, 9, 0)
    event = _event("FREQ=DAILY;COUNT=4", start, exdates=(_utc(2026, 11, 3, 9, 0),))
    assert [moment.day for moment in _starts([event], _utc(2026, 11, 1), _utc(2026, 12, 1))] == [2, 4, 5]


def test_exdate_with_tzid_from_ics() -> None:
    text = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "UID:standup",
            "DTSTART;TZID=America/New_York:20261102T090000",
            "DTEND;TZID=America/New_York:20261102T091500",
            "RRULE:FREQ=WEEKLY;COUNT=3",
            "EXDATE;TZID=America/New_York:20261109T090000",
            "SUMMARY:Standup",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    events = ics.parse_calendar(text, UTC).events
    found = _starts(events, _utc(2026, 11, 1), _utc(2026, 12, 1))
    assert [(moment.month, moment.day, moment.hour) for moment in found] == [(11, 2, 9), (11, 16, 9)]


def test_recurrence_id_override_replaces_its_instance() -> None:
    text = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "UID:weekly-sync",
            "DTSTART:20261102T150000Z",
            "DTEND:20261102T153000Z",
            "RRULE:FREQ=WEEKLY;COUNT=4",
            "SUMMARY:Weekly sync",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:weekly-sync",
            "RECURRENCE-ID:20261109T150000Z",
            "DTSTART:20261110T170000Z",
            "DTEND:20261110T173000Z",
            "SUMMARY:Weekly sync (moved)",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:weekly-sync",
            "RECURRENCE-ID:20261116T150000Z",
            "DTSTART:20261116T150000Z",
            "DTEND:20261116T153000Z",
            "STATUS:CANCELLED",
            "SUMMARY:Weekly sync",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    found, warnings = rrule.expand(ics.parse_calendar(text, UTC).events, _utc(2026, 11, 1), _utc(2026, 12, 1))
    assert warnings == []
    assert [(item.start, item.event.summary) for item in found] == [
        (_utc(2026, 11, 2, 15, 0), "Weekly sync"),
        (_utc(2026, 11, 10, 17, 0), "Weekly sync (moved)"),
        (_utc(2026, 11, 23, 15, 0), "Weekly sync"),
    ]


def test_weekly_series_keeps_its_wall_time_when_daylight_saving_ends() -> None:
    start = datetime(2026, 10, 26, 9, 0, tzinfo=NEW_YORK)
    found = _starts([_event("FREQ=WEEKLY;COUNT=2", start)], _utc(2026, 10, 1), _utc(2026, 12, 1))
    assert [moment.astimezone(NEW_YORK).hour for moment in found] == [9, 9]
    assert [moment.astimezone(UTC).hour for moment in found] == [13, 14]


def test_wall_time_in_the_spring_forward_gap_uses_the_offset_before_the_gap() -> None:
    start = datetime(2026, 3, 7, 2, 30, tzinfo=NEW_YORK)
    found = _starts([_event("FREQ=DAILY;COUNT=2", start)], _utc(2026, 3, 1), _utc(2026, 4, 1))
    assert [moment.astimezone(UTC) for moment in found] == [_utc(2026, 3, 7, 7, 30), _utc(2026, 3, 8, 7, 30)]


def test_all_day_series_keeps_its_length_in_days() -> None:
    start = datetime(2026, 11, 2, tzinfo=NEW_YORK)
    event = ics.Event(
        uid="trip",
        summary="Trip",
        start=start,
        end=start + timedelta(days=2),
        all_day=True,
        rrule="FREQ=WEEKLY",
    )
    found, _ = rrule.expand(
        [event], datetime(2026, 11, 8, tzinfo=NEW_YORK), datetime(2026, 11, 12, tzinfo=NEW_YORK)
    )
    assert [(item.start.day, item.end.day) for item in found] == [(9, 11)]


@pytest.mark.parametrize(
    ("rule", "part"),
    [
        ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", "BYSETPOS"),
        ("FREQ=YEARLY;BYWEEKNO=20", "BYWEEKNO"),
        ("FREQ=HOURLY;COUNT=5", "HOURLY"),
        ("FREQ=DAILY;INTERVAL=0", "INTERVAL"),
    ],
)
def test_unsupported_rules_warn_and_keep_only_the_first_instance(rule: str, part: str) -> None:
    start = _utc(2026, 11, 30, 9, 0)
    found, warnings = rrule.expand(
        [_event(rule, start, summary="Odd rule")], _utc(2026, 11, 1), _utc(2027, 3, 1)
    )
    assert [item.start for item in found] == [start]
    assert len(warnings) == 1
    assert part in warnings[0]
    assert "first instance" in warnings[0]


def test_expansion_stops_at_the_instance_cap() -> None:
    start = _utc(2026, 1, 1, 9, 0)
    found, warnings = rrule.expand([_event("FREQ=DAILY", start)], _utc(2026, 1, 1), _utc(2030, 1, 1))
    assert len(found) == rrule.MAX_INSTANCES == 1000
    assert len(warnings) == 1
    assert "1000" in warnings[0]


def test_far_window_is_reached_without_walking_every_period() -> None:
    start = _utc(1990, 1, 1, 9, 0)
    found = _starts([_event("FREQ=DAILY", start)], _utc(2026, 11, 12), _utc(2026, 11, 14))
    assert [moment.date().isoformat() for moment in found] == ["2026-11-12", "2026-11-13"]
