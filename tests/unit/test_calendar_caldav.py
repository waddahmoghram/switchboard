"""Unit tests for CalDAV discovery, calendar-query, and PUT over a fake HTTP transport."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from switchboard.connectors._dav import DavClient, DavError
from switchboard.connectors.calendar import caldav
from tests.fakes import FakeHttp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
ROOT = "https://dav.example.invalid/"
HOME = "https://dav.example.invalid/dav/calendars/owner/"
PERSONAL = HOME + "personal/"


def _client(http: FakeHttp, url: str = ROOT) -> DavClient:
    return DavClient(http, url, "owner@example.invalid", "app-password", "calendar source 'work'")


def _xml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_discovery_follows_the_well_known_redirect_to_the_principal_and_home(fake_http: FakeHttp) -> None:
    fake_http.add("PROPFIND", ROOT, status=404)
    fake_http.add("PROPFIND", ROOT + ".well-known/caldav", status=301, headers={"Location": "/"})
    fake_http.add("PROPFIND", ROOT, status=207, body=_xml("principal.xml"))
    fake_http.add("PROPFIND", ROOT + "dav/principals/owner/", status=207, body=_xml("home_set.xml"))
    fake_http.add("PROPFIND", HOME, status=207, body=_xml("propfind_home.xml"))

    calendars = caldav.find_calendars(_client(fake_http))

    assert calendars == [caldav.CalendarCollection(PERSONAL, "Personal")]
    assert [(request.url, request.headers["depth"]) for request in fake_http.requests] == [
        (ROOT, "0"),
        (ROOT + ".well-known/caldav", "0"),
        (ROOT, "0"),
        (ROOT + "dav/principals/owner/", "0"),
        (HOME, "1"),
    ]


def test_a_calendar_url_is_used_directly(fake_http: FakeHttp) -> None:
    single = (
        b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>'
        b"<d:href>/dav/calendars/owner/personal/</d:href><d:propstat><d:prop>"
        b"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Personal</d:displayname>"
        b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    )
    fake_http.add("PROPFIND", PERSONAL, status=207, body=single)
    assert caldav.find_calendars(_client(fake_http, PERSONAL)) == [
        caldav.CalendarCollection(PERSONAL, "Personal")
    ]
    assert len(fake_http.requests) == 1


def test_calendar_query_asks_for_events_in_a_utc_time_range(fake_http: FakeHttp) -> None:
    fake_http.add("REPORT", PERSONAL, status=207, body=_xml("report_events.xml"))
    texts = caldav.query_events(
        _client(fake_http),
        PERSONAL,
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert len(texts) == 2
    assert all(text.startswith("BEGIN:VCALENDAR") for text in texts)
    [request] = fake_http.requests
    assert request.headers["depth"] == "1"
    body = (request.body or b"").decode()
    assert '<C:comp-filter name="VEVENT">' in body
    assert re.search(r'<C:time-range start="20261101T000000Z" end="20261201T000000Z"/>', body)


def test_put_event_creates_uid_dot_ics_without_overwriting(fake_http: FakeHttp) -> None:
    fake_http.add("PUT", PERSONAL + "abc-123.ics", status=201)
    url = caldav.put_event(
        _client(fake_http), PERSONAL.rstrip("/"), "abc-123", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    )
    assert url == PERSONAL + "abc-123.ics"
    [request] = fake_http.requests
    assert request.headers["if-none-match"] == "*"
    assert request.headers["content-type"] == "text/calendar; charset=utf-8"


def test_no_event_calendars_is_an_error(fake_http: FakeHttp) -> None:
    only_tasks = _xml("propfind_home.xml").replace(b'name="VEVENT"', b'name="VTODO"')
    fake_http.add("PROPFIND", ROOT, status=207, body=_xml("principal.xml"))
    fake_http.add("PROPFIND", ROOT + "dav/principals/owner/", status=207, body=_xml("home_set.xml"))
    fake_http.add("PROPFIND", HOME, status=207, body=only_tasks)
    with pytest.raises(DavError, match="no calendar that holds events"):
        caldav.find_calendars(_client(fake_http))
