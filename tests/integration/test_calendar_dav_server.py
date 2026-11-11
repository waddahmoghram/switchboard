"""Integration tests: the calendar connector against a local fake CalDAV server over real HTTP.

The server binds 127.0.0.1 on an ephemeral port, answers PROPFIND and REPORT from canned XML, keeps
PUT bodies in memory for GET, and records every request. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App, build_app
from switchboard.config import config_from_dict
from switchboard.connectors._dav import DavClient
from switchboard.connectors.calendar import ics
from switchboard.connectors.calendar.service import CalendarService
from switchboard.connectors.calendar.settings import load_settings
from switchboard.httpclient import HttpClient
from switchboard.policy import Origin
from tests.fakes import FakeMailService

pytestmark = pytest.mark.integration

AppFactory = Callable[..., App]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
USERNAME = "owner"
PASSWORD = "app-password-for-tests"
AUTHORIZATION = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode("ascii")
PERSONAL = "/dav/calendars/owner/personal/"
TEAM = "/dav/calendars/owner/team/"
DAY = {"start": "2026-11-12", "end": "2026-11-12"}


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class FakeDavServer(ThreadingHTTPServer):
    """Canned replies keyed by (method, path); PUT stores bodies that GET returns."""

    def __init__(self, routes: dict[tuple[str, str], Reply]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes = routes
        self.seen: list[Seen] = []
        self.stored: dict[str, bytes] = {}

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeDavServer

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.server.seen.append(Seen(self.command, self.path, headers, body))
        if headers.get("authorization") != AUTHORIZATION:
            self._reply(Reply(401))
        elif self.command == "PUT":
            self._reply(self._put(headers, body))
        elif self.command == "GET":
            stored = self.server.stored.get(self.path)
            self._reply(
                Reply(404) if stored is None else Reply(200, stored, {"Content-Type": "text/calendar"})
            )
        else:
            self._reply(self.server.routes.get((self.command, self.path), Reply(404)))

    def _put(self, headers: dict[str, str], body: bytes) -> Reply:
        if headers.get("if-none-match") == "*" and self.path in self.server.stored:
            return Reply(412)
        self.server.stored[self.path] = body
        return Reply(201)

    def _reply(self, reply: Reply) -> None:
        self.send_response(reply.status)
        headers = {"Content-Type": "application/xml; charset=utf-8", **reply.headers}
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    do_PROPFIND = do_REPORT = do_PUT = do_GET = _serve

    def log_message(self, format: str, *args: Any) -> None:
        return  # keep test output quiet


def _xml(name: str) -> Reply:
    return Reply(207, (FIXTURES / name).read_bytes())


@pytest.fixture
def dav_server() -> Iterator[FakeDavServer]:
    server = FakeDavServer(
        {
            ("PROPFIND", "/"): _xml("principal.xml"),
            ("PROPFIND", "/dav/principals/owner/"): _xml("home_set.xml"),
            ("PROPFIND", "/dav/calendars/owner/"): _xml("propfind_home.xml"),
            ("REPORT", PERSONAL): _xml("report_events.xml"),
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dav_app(dav_server: FakeDavServer, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    """A full app whose HTTP transport is the real client, pointed at the loopback server."""
    monkeypatch.setenv("SB_TEST_DAV_PASSWORD", PASSWORD)
    work = {
        "type": "caldav",
        "url": dav_server.url + "/",
        "username": USERNAME,
        "password": "env:SB_TEST_DAV_PASSWORD",
        "writable": True,
    }
    calendar = {
        "timezone": "America/New_York",
        "owner_email": "owner@example.invalid",
        "sources": {"work": work},
    }
    config = {"switchboard": {"connectors": ["calendar"]}, "connectors": {"calendar": calendar}}
    return build_app(config_from_dict(config, data_dir), http=HttpClient(timeout=5))


def test_agenda_discovers_the_calendar_and_reads_events(dav_app: App, dav_server: FakeDavServer) -> None:
    outcome = dav_app.call("calendar_agenda", DAY, Origin.ASSISTANT)

    assert outcome.status == "ok", outcome.message
    assert [(event["summary"], event["start"]) for event in outcome.data["events"]] == [
        ("Standup", "2026-11-12T09:30:00-05:00"),
        ("Quarterly review", "2026-11-12T14:00:00-05:00"),
    ]
    assert outcome.data["events"][0]["recurring"] is True
    assert [(seen.method, seen.path, seen.headers.get("depth")) for seen in dav_server.seen] == [
        ("PROPFIND", "/", "0"),
        ("PROPFIND", "/dav/principals/owner/", "0"),
        ("PROPFIND", "/dav/calendars/owner/", "1"),
        ("REPORT", PERSONAL, "1"),
    ]
    report = dav_server.seen[-1].body.decode("utf-8")
    assert '<C:comp-filter name="VEVENT">' in report
    assert re.search(r'<C:time-range start="\d{8}T\d{6}Z" end="\d{8}T\d{6}Z"/>', report)

    again = dav_app.call("calendar_agenda", DAY, Origin.ASSISTANT)
    assert again.data == outcome.data
    assert len(dav_server.seen) == 4


def _add_deleted_team_calendar(server: FakeDavServer) -> None:
    """List a second calendar that answers 404, as after it is deleted on another device."""
    team = (
        f"<d:response><d:href>{TEAM}</d:href><d:propstat><d:prop>"
        "<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>"
        "<d:displayname>Team</d:displayname>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    ).encode()
    listing = (FIXTURES / "propfind_home.xml").read_bytes()
    server.routes[("PROPFIND", "/dav/calendars/owner/")] = Reply(
        207, listing.replace(b"</d:multistatus>", team + b"</d:multistatus>")
    )
    server.routes[("REPORT", TEAM)] = Reply(404)


def test_a_deleted_calendar_is_skipped_and_discovery_runs_again(
    dav_app: App, dav_server: FakeDavServer
) -> None:
    _add_deleted_team_calendar(dav_server)
    service = CalendarService(dav_app.ctx, load_settings(dav_app.ctx.connector_config("calendar")))
    now = datetime.now(UTC)

    state = service.refresh("work", now)

    assert state["error"] is None
    [note] = state["skipped"]
    assert "skipped calendar 'Team'" in note and "HTTP 404" in note
    assert [seen.path for seen in dav_server.seen if seen.method == "REPORT"] == [PERSONAL, TEAM]

    outcome = dav_app.call("calendar_agenda", DAY, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    assert [event["summary"] for event in outcome.data["events"]] == ["Standup", "Quarterly review"]
    assert any("skipped calendar 'Team'" in warning for warning in outcome.data["warnings"])

    dav_server.seen.clear()
    service.refresh("work", now + timedelta(hours=1))
    assert (dav_server.seen[0].method, dav_server.seen[0].path) == ("PROPFIND", "/")


def test_every_calendar_gone_keeps_the_cached_events_and_reports_the_error(
    dav_app: App, dav_server: FakeDavServer
) -> None:
    service = CalendarService(dav_app.ctx, load_settings(dav_app.ctx.connector_config("calendar")))
    now = datetime.now(UTC)
    assert service.refresh("work", now)["error"] is None

    dav_server.routes[("REPORT", PERSONAL)] = Reply(410)
    state = service.refresh("work", now + timedelta(hours=1))

    assert "HTTP 410" in state["error"]
    assert state["events"] > 0
    dav_server.seen.clear()
    service.refresh("work", now + timedelta(hours=2))
    assert (dav_server.seen[0].method, dav_server.seen[0].path) == ("PROPFIND", "/")


def test_create_event_puts_only_after_approval(dav_app: App, dav_server: FakeDavServer) -> None:
    args = {"summary": "Design review", "start": "2026-11-20T10:00", "duration_minutes": 45, "source": "work"}
    pending = dav_app.call("calendar_create_event", args, Origin.ASSISTANT)
    assert pending.status == "pending" and pending.approval_id is not None
    assert dav_server.seen == []

    done = dav_app.dispatcher.approve(pending.approval_id)

    assert done.status == "ok", done.message
    assert (done.data["status"], done.data["calendar"]) == ("created", "Personal")
    [put] = [seen for seen in dav_server.seen if seen.method == "PUT"]
    assert put.path == f"{PERSONAL}{done.data['uid']}.ics"
    assert put.headers["if-none-match"] == "*"
    assert put.headers["content-type"] == "text/calendar; charset=utf-8"
    text = put.body.decode("utf-8")
    assert "METHOD:" not in text
    [event] = ics.parse_calendar(text, UTC).events
    assert event.summary == "Design review"
    assert event.start == datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    assert event.end - event.start == timedelta(minutes=45)

    client = DavClient(HttpClient(timeout=5), dav_server.url + "/", USERNAME, PASSWORD, "test")
    assert client.get(dav_server.url + put.path).body == put.body


def test_a_wrong_password_is_reported_without_leaking_it(
    dav_app: App, dav_server: FakeDavServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_DAV_PASSWORD", "not-the-password")
    outcome = dav_app.call("calendar_agenda", DAY, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    assert outcome.data["events"] == []
    [warning] = outcome.data["warnings"]
    assert "HTTP 401" in warning and "app-specific password" in warning
    assert "not-the-password" not in warning
    assert [seen.path for seen in dav_server.seen] == ["/"]


def test_propose_event_saves_an_imip_draft_through_the_mail_service(make_app: AppFactory) -> None:
    calendar = {
        "timezone": "America/New_York",
        "owner_email": "owner@example.invalid",
        "invite_account": "personal",
    }
    app = make_app({"switchboard": {"connectors": ["calendar"]}, "connectors": {"calendar": calendar}})
    mail = FakeMailService()
    app.ctx.provide("mail.drafts", mail)
    args = {
        "summary": "Lunch with Sam",
        "start": "2026-11-20T12:00",
        "end": "2026-11-20T13:00",
        "location": "Cafe",
        "attendees": ["sam@example.invalid"],
    }

    outcome = app.call("calendar_propose_event", args, Origin.ASSISTANT)

    assert outcome.status == "ok", outcome.message
    assert (outcome.data["status"], outcome.data["draft"]) == ("draft_saved", "draft-1")
    assert mail.sent == []
    [(account, message)] = mail.drafts
    assert account == "personal"
    assert str(message["To"]) == "owner@example.invalid"
    [calendar_part] = [part for part in message.walk() if part.get_content_type() == "text/calendar"]
    assert calendar_part.get_param("method") == "REQUEST"
    roots, _ = ics.parse_components(calendar_part.get_content())
    assert roots[0].text("METHOD") == "REQUEST"
    vevent = roots[0].components[0]
    assert vevent.text("SUMMARY") == "Lunch with Sam"
    attendees = vevent.all("ATTENDEE")
    assert [prop.value for prop in attendees] == ["mailto:sam@example.invalid"]
    assert attendees[0].params["RSVP"] == "TRUE"
    organizer = vevent.first("ORGANIZER")
    assert organizer is not None and organizer.value == "mailto:owner@example.invalid"
