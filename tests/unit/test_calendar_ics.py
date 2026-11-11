"""Unit tests for the iCalendar reader and writer (RFC 5545 subset)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from switchboard.connectors.calendar import ics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"

try:
    NEW_YORK = ZoneInfo("America/New_York")
    BERLIN = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:  # pragma: no cover - only on systems without IANA time zone data
    pytest.skip("IANA time zone data is not installed", allow_module_level=True)


def test_unfold_joins_continuation_lines() -> None:
    text = "DESCRIPTION:This is a lo\r\n ng description\r\n\tthat continues\r\nSUMMARY:x\r\n"
    assert ics.unfold(text) == ["DESCRIPTION:This is a long descriptionthat continues", "SUMMARY:x"]


def test_parse_line_keeps_colons_and_semicolons_inside_quoted_parameters() -> None:
    prop = ics.parse_line(
        'ORGANIZER;CN="Doe, Jane: Chair";SENT-BY="mailto:a@example.invalid":mailto:jane@example.invalid'
    )
    assert prop.name == "ORGANIZER"
    assert prop.params == {"CN": "Doe, Jane: Chair", "SENT-BY": "mailto:a@example.invalid"}
    assert prop.value == "mailto:jane@example.invalid"


def test_parse_line_rejects_a_line_without_a_value() -> None:
    with pytest.raises(ValueError, match="content line"):
        ics.parse_line("SUMMARY")


def test_text_escapes() -> None:
    assert ics.unescape_text("Lunch\\, then\\; review\\nline two \\\\ done\\N!") == (
        "Lunch, then; review\nline two \\ done\n!"
    )
    assert ics.escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


def test_fixture_events() -> None:
    parsed = ics.parse_calendar((FIXTURES / "basic.ics").read_text(encoding="utf-8"), NEW_YORK)
    assert parsed.name == "Synthetic Personal"
    by_uid = {event.uid: event for event in parsed.events}
    assert sorted(by_uid) == [
        "all-day@example.invalid",
        "floating@example.invalid",
        "timed-berlin@example.invalid",
        "unknown-zone@example.invalid",
        "utc-call@example.invalid",
    ]

    timed = by_uid["timed-berlin@example.invalid"]
    assert ics.zone_key(timed.zone) == "Europe/Berlin"
    assert timed.start == datetime(2026, 11, 12, 9, 0, tzinfo=BERLIN)
    assert timed.end == datetime(2026, 11, 12, 10, 0, tzinfo=BERLIN)
    assert timed.summary == "Planning, review; and notes"
    assert timed.description == "First line\nSecond line with a long text that is folded across two lines"
    assert timed.location == "Room 4, Building B"
    assert timed.organizer == "organizer@example.invalid"
    assert timed.attendees == ("guest1@example.invalid", "guest2@example.invalid")
    assert timed.status == "CONFIRMED"
    assert not timed.all_day

    all_day = by_uid["all-day@example.invalid"]
    assert all_day.all_day and all_day.transparent
    assert all_day.start == datetime(2026, 11, 13, tzinfo=NEW_YORK)
    assert all_day.end == datetime(2026, 11, 15, tzinfo=NEW_YORK)

    floating = by_uid["floating@example.invalid"]
    assert floating.start == datetime(2026, 11, 12, 12, 0, tzinfo=NEW_YORK)
    assert floating.end == datetime(2026, 11, 12, 13, 30, tzinfo=NEW_YORK)

    assert by_uid["utc-call@example.invalid"].start == datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def test_unknown_tzid_falls_back_to_the_calendar_zone_with_a_warning() -> None:
    parsed = ics.parse_calendar((FIXTURES / "basic.ics").read_text(encoding="utf-8"), NEW_YORK)
    unknown = next(event for event in parsed.events if event.uid == "unknown-zone@example.invalid")
    assert unknown.start == datetime(2026, 11, 12, 8, 0, tzinfo=NEW_YORK)
    assert any(
        "Mars/Olympus_Mons" in warning and "America/New_York" in warning for warning in parsed.warnings
    )
    assert "skipped an event without DTSTART" in parsed.warnings


def test_vendor_prefixed_tzid_resolves_to_the_iana_zone() -> None:
    assert ics.zone_named("/mozilla.org/20050126_1/America/New_York") is NEW_YORK
    assert ics.zone_named("UTC") is UTC
    assert ics.zone_named("Eastern Standard Time") is None


@pytest.mark.parametrize(
    ("text", "days", "seconds"),
    [("PT1H30M", 0, 5400), ("P1D", 1, 0), ("P2W", 14, 0), ("-PT15M", 0, -900), ("P1DT2H", 1, 7200)],
)
def test_parse_duration(text: str, days: int, seconds: int) -> None:
    assert ics.parse_duration(text) == ics.Duration(days, seconds)


@pytest.mark.parametrize("text", ["P", "PT", "1H", "P1H"])
def test_parse_duration_rejects_malformed_values(text: str) -> None:
    with pytest.raises(ValueError, match="DURATION"):
        ics.parse_duration(text)


def test_day_durations_are_nominal_and_hour_durations_exact_across_dst() -> None:
    start = datetime(2026, 10, 31, 9, 0, tzinfo=NEW_YORK)  # the clocks go back on 2026-11-01
    assert ics.add_duration(start, ics.Duration(1, 0)) == datetime(2026, 11, 1, 9, 0, tzinfo=NEW_YORK)
    exact = ics.add_duration(start, ics.Duration(0, 24 * 3600))
    assert exact.astimezone(UTC) == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    assert (exact.hour, exact.utcoffset()) == (8, timedelta(hours=-5))


def test_missing_end_defaults() -> None:
    text = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART;VALUE=DATE:20261120",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:b",
            "DTSTART:20261120T100000Z",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    day, moment = ics.parse_calendar(text, NEW_YORK).events
    assert day.end - day.start == timedelta(days=1)
    assert moment.end == moment.start


def test_serialize_round_trips_and_folds_at_75_octets() -> None:
    event = ics.Event(
        uid="0f8e7d6c-round-trip",
        summary="Café review, part 1; notes\\draft",
        start=datetime(2026, 11, 12, 14, 0, tzinfo=UTC),
        end=datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
        description="Line one\n" + "é" * 80,
        location="Room 1",
        organizer="owner@example.invalid",
        attendees=("guest@example.invalid",),
    )
    text = ics.serialize(event, stamp=datetime(2026, 11, 11, 13, 0, tzinfo=UTC), method="REQUEST")
    physical = text.split("\r\n")
    assert physical[-1] == ""
    assert "\n" not in text.replace("\r\n", "")
    assert all(len(line.encode("utf-8")) <= 75 for line in physical)
    parsed = ics.parse_calendar(text, UTC)
    assert parsed.warnings == []
    [back] = parsed.events
    assert back.summary == event.summary
    assert back.description == event.description
    assert (back.start, back.end) == (event.start, event.end)
    assert back.location == "Room 1"
    assert back.organizer == "owner@example.invalid"
    assert back.attendees == ("guest@example.invalid",)


def test_fold_never_splits_a_multibyte_character() -> None:
    line = "SUMMARY:" + "日本語" * 40
    pieces = ics.fold(line).split("\r\n")
    assert len(pieces) > 1
    assert all(len(piece.encode("utf-8")) <= 75 for piece in pieces)
    assert pieces[0] + "".join(piece[1:] for piece in pieces[1:]) == line


def test_serialize_all_day_event_uses_dates_and_no_method() -> None:
    event = ics.Event(
        uid="all-day-1",
        summary="Offsite",
        start=datetime(2026, 11, 20, tzinfo=NEW_YORK),
        end=datetime(2026, 11, 22, tzinfo=NEW_YORK),
        all_day=True,
    )
    text = ics.serialize(event, stamp=datetime(2026, 11, 11, tzinfo=UTC))
    assert "DTSTART;VALUE=DATE:20261120\r\n" in text
    assert "DTEND;VALUE=DATE:20261122\r\n" in text
    assert "METHOD:" not in text


def test_event_json_round_trip() -> None:
    parsed = ics.parse_calendar((FIXTURES / "basic.ics").read_text(encoding="utf-8"), NEW_YORK)
    for event in parsed.events:
        assert ics.Event.from_json(event.to_json()) == event
