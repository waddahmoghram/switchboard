"""Unit tests for iMIP invitation messages (RFC 6047 carrying an RFC 5546 REQUEST)."""

from __future__ import annotations

from datetime import UTC, datetime

from switchboard.connectors.calendar import ics, imip

STAMP = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)


def _event() -> ics.Event:
    return ics.Event(
        uid="1b2c3d4e-0000-4000-8000-000000000001",
        summary="Design review",
        start=datetime(2026, 11, 12, 14, 0, tzinfo=UTC),
        end=datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
        location="Room 2",
        organizer="owner@example.invalid",
        attendees=("alice@example.invalid", "bob@example.invalid"),
    )


def test_request_has_a_plain_part_and_a_calendar_part_with_method_request() -> None:
    recipients = ["alice@example.invalid", "bob@example.invalid"]
    message = imip.build_request(
        _event(), organizer="owner@example.invalid", recipients=recipients, stamp=STAMP, zone=UTC
    )
    assert message.get_content_type() == "multipart/alternative"
    plain, calendar = list(message.iter_parts())
    assert plain.get_content_type() == "text/plain"
    assert "Design review" in plain.get_content()
    assert calendar.get_content_type() == "text/calendar"
    assert calendar.get_param("method") == "REQUEST"
    assert str(message["From"]) == "owner@example.invalid"
    assert str(message["To"]) == "alice@example.invalid, bob@example.invalid"
    assert str(message["Subject"]).startswith("Invitation: Design review")
    assert str(message["Message-ID"]).endswith("@example.invalid>")


def test_calendar_part_carries_the_itip_request_properties() -> None:
    message = imip.build_request(
        _event(),
        organizer="owner@example.invalid",
        recipients=["alice@example.invalid"],
        stamp=STAMP,
        zone=UTC,
    )
    calendar = next(part for part in message.walk() if part.get_content_type() == "text/calendar")
    roots, warnings = ics.parse_components(calendar.get_content())
    assert warnings == []
    [vcalendar] = roots
    assert vcalendar.text("METHOD") == "REQUEST"
    [vevent] = vcalendar.components
    assert vevent.text("UID") == "1b2c3d4e-0000-4000-8000-000000000001"
    assert vevent.text("DTSTAMP") == "20261111T130000Z"
    assert vevent.text("SEQUENCE") == "0"
    assert vevent.text("DTSTART") == "20261112T140000Z"
    organizer = vevent.first("ORGANIZER")
    assert organizer is not None and organizer.value == "mailto:owner@example.invalid"
    attendees = vevent.all("ATTENDEE")
    assert [prop.value for prop in attendees] == [
        "mailto:alice@example.invalid",
        "mailto:bob@example.invalid",
    ]
    assert all(prop.params["RSVP"] == "TRUE" for prop in attendees)


def test_describe_when_for_all_day_and_timed_events() -> None:
    event = _event()
    assert imip.describe_when(event, UTC) == "2026-11-12 14:00-15:00 (UTC)"
    all_day = ics.Event(
        uid="x",
        summary="Trip",
        start=datetime(2026, 11, 13, tzinfo=UTC),
        end=datetime(2026, 11, 15, tzinfo=UTC),
        all_day=True,
    )
    assert imip.describe_when(all_day, UTC) == "2026-11-13, all day (2 days)"
