"""Unit tests for the calendar connector: settings, tools, cache, refresh job, and policy wiring."""

from __future__ import annotations

import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from switchboard import httpclient
from switchboard.app import App
from switchboard.connectors.calendar.service import CalendarService, parse_moment, webcal_to_https
from switchboard.connectors.calendar.settings import load_settings
from switchboard.errors import ConfigError, ToolError
from switchboard.policy import Origin, Tier
from tests.fakes import FakeHttp, FakeMailService

try:
    NEW_YORK = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - only on systems without IANA time zone data
    pytest.skip("IANA time zone data is not installed", allow_module_level=True)

AppFactory = Callable[..., App]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
FEED = "https://calendar.example.invalid/private-0000/basic.ics"
DAY = {"start": "2026-11-12", "end": "2026-11-12"}
UPCOMING = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:upcoming-1@example.invalid
DTSTART:20261112T140000Z
DTEND:20261112T143000Z
SUMMARY:Dentist
END:VEVENT
BEGIN:VEVENT
UID:later@example.invalid
DTSTART:20261112T150000Z
DTEND:20261112T153000Z
SUMMARY:Later meeting
END:VEVENT
BEGIN:VEVENT
UID:holiday@example.invalid
DTSTART;VALUE=DATE:20261112
SUMMARY:Holiday
END:VEVENT
END:VCALENDAR
"""


def _config(sources: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    calendar = {
        "timezone": "America/New_York",
        "owner_email": "owner@example.invalid",
        "sources": sources or {},
        **extra,
    }
    return {"switchboard": {"connectors": ["calendar"]}, "connectors": {"calendar": calendar}}


def _file_source(path: Path) -> dict[str, Any]:
    return {"local": {"type": "ics_file", "path": str(path)}}


def _feed_app(make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> App:
    monkeypatch.setenv("SB_TEST_CALENDAR_URL", "webcal://calendar.example.invalid/private-0000/basic.ics")
    fake_http.add("GET", FEED, body=(FIXTURES / "basic.ics").read_bytes(), headers={"ETag": '"v1"'})
    return make_app(_config({"google": {"type": "ics_url", "url": "env:SB_TEST_CALENDAR_URL"}}))


def _service(app: App) -> CalendarService:
    return CalendarService(app.ctx, load_settings(app.ctx.connector_config("calendar")))


def test_register_adds_bounded_tools_and_the_refresh_job(make_app: AppFactory) -> None:
    app = make_app(_config())
    tools = [tool for tool in app.registry.all() if tool.connector == "calendar"]
    assert {tool.name: tool.tier for tool in tools} == {
        "calendar_sources": Tier.READ,
        "calendar_agenda": Tier.READ,
        "calendar_free_slots": Tier.READ,
        "calendar_propose_event": Tier.DRAFT,
        "calendar_create_event": Tier.ACT,
        "calendar_send_invite": Tier.ACT,
    }
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
    assert "calendar.refresh" in [job.name for job in app.ctx.scheduler.jobs()]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            {"type": "ics_url", "url": "https://calendar.example.invalid/private/basic.ics"},
            "secret reference",
        ),
        (
            {
                "type": "caldav",
                "url": "https://dav.example.invalid/",
                "username": "o",
                "password": "plain-text",
            },
            "secret reference",
        ),
        (
            {"type": "caldav", "url": "http://dav.example.invalid/", "username": "o", "password": "env:X"},
            "https://",
        ),
        ({"type": "webcal", "url": "env:X"}, "type must be one of"),
    ],
)
def test_invalid_sources_are_rejected_at_startup(
    make_app: AppFactory, source: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        make_app(_config({"bad": source}))


def test_unknown_timezone_is_rejected(make_app: AppFactory) -> None:
    with pytest.raises(ConfigError, match="IANA"):
        make_app(_config(timezone="Mars/Olympus_Mons"))


def test_agenda_downloads_the_feed_once_and_renders_local_times(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _feed_app(make_app, fake_http, monkeypatch)
    first = app.call("calendar_agenda", DAY, Origin.ASSISTANT)
    second = app.call("calendar_agenda", DAY, Origin.ASSISTANT)

    assert first.status == "ok", first.message
    assert second.data == first.data
    [request] = fake_http.requests
    assert request.headers["accept"] == "text/calendar"
    data = first.data
    assert data["untrusted_content"] is True
    assert data["time_zone"] == "America/New_York"
    assert [event["summary"] for event in data["events"]] == [
        "Planning, review; and notes",
        "Unknown zone sync",
        "UTC call",
        "Floating lunch",
    ]
    assert data["events"][0]["start"] == "2026-11-12T03:00:00-05:00"
    assert data["events"][0]["attendee_count"] == 2
    assert data["events"][2]["start"] == "2026-11-12T10:00:00-05:00"
    assert data["warnings"] == []
    assert "private-0000" not in repr(first.to_dict())


def test_refresh_sends_the_stored_etag_and_keeps_the_cache_on_304(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _feed_app(make_app, fake_http, monkeypatch)
    assert app.call("calendar_agenda", DAY, Origin.ASSISTANT).status == "ok"
    fake_http.add("GET", FEED, status=304)

    state = _service(app).refresh("google", datetime.now(UTC))

    assert fake_http.requests[-1].headers["if-none-match"] == '"v1"'
    assert state["error"] is None
    assert state["events"] == 5
    listed = app.call("calendar_sources", {}, Origin.ASSISTANT).data["sources"]
    assert listed == [
        {
            "name": "google",
            "type": "ics_url",
            "writable": False,
            "fetched_at": state["fetched_at"],
            "events": 5,
            "error": None,
            "warnings": 2,
        }
    ]


def test_a_failing_feed_keeps_cached_events_and_warns(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _feed_app(make_app, fake_http, monkeypatch)
    assert app.call("calendar_agenda", DAY, Origin.ASSISTANT).status == "ok"
    fake_http.add("GET", FEED, status=500)
    _service(app).refresh("google", datetime.now(UTC))

    outcome = app.call("calendar_agenda", DAY, Origin.ASSISTANT)

    assert outcome.status == "ok"
    assert len(outcome.data["events"]) == 4
    [warning] = outcome.data["warnings"]
    assert "HTTP 500" in warning and "showing cached events" in warning
    assert "private-0000" not in warning


def test_retry_after_pauses_a_throttled_feed(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_CALENDAR_URL", FEED)
    fake_http.add("GET", FEED, status=429, headers={"Retry-After": "600"})
    app = make_app(_config({"google": {"type": "ics_url", "url": "env:SB_TEST_CALENDAR_URL"}}))
    service = _service(app)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    state = service.refresh("google", now)

    assert "HTTP 429" in state["error"]
    assert state["retry_at"] == "2026-11-12T12:10:00+00:00"
    assert service.refresh("google", now + timedelta(minutes=5)) == state
    assert len(fake_http.requests) == 1


def test_agenda_caps_long_descriptions(make_app: AppFactory, tmp_path: Path) -> None:
    feed = tmp_path / "long.ics"
    feed.write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:long\nDTSTART:20261112T140000Z\nDTEND:20261112T150000Z\n"
        f"SUMMARY:Long\nDESCRIPTION:{'x' * 3000}\nEND:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )
    app = make_app(_config(_file_source(feed)))
    [event] = app.call("calendar_agenda", DAY, Origin.ASSISTANT).data["events"]
    assert len(event["description"]) == 500
    assert event["description"].endswith("…")


def test_agenda_window_must_be_ordered_and_bounded(make_app: AppFactory) -> None:
    app = make_app(_config(_file_source(FIXTURES / "basic.ics")))
    backwards = app.call(
        "calendar_agenda", {"start": "2026-11-13", "end": "2026-11-12T00:00"}, Origin.ASSISTANT
    )
    assert backwards.status == "error" and "after start" in backwards.message
    huge = app.call("calendar_agenda", {"start": "2026-01-01", "end": "2028-01-01"}, Origin.ASSISTANT)
    assert huge.status == "error" and "366 days" in huge.message
    unbounded = app.call("calendar_agenda", {**DAY, "limit": 500}, Origin.ASSISTANT)
    unknown = app.call("calendar_agenda", {**DAY, "url": "https://example.invalid/"}, Origin.ASSISTANT)
    assert unbounded.status == "invalid" and unknown.status == "invalid"


def test_refresh_job_emits_upcoming_events_once(make_app: AppFactory, tmp_path: Path) -> None:
    feed = tmp_path / "upcoming.ics"
    feed.write_text(UPCOMING, encoding="utf-8")
    app = make_app(_config(_file_source(feed)))
    service = _service(app)
    now = datetime(2026, 11, 12, 13, 45, tzinfo=UTC)

    service.refresh_all(now)
    service.refresh_all(now + timedelta(minutes=5))

    upcoming = [event for event in app.ctx.events.recent(20) if event.type == "calendar.upcoming"]
    assert len(upcoming) == 1
    assert upcoming[0].source == "calendar"
    assert upcoming[0].payload == {
        "calendar": "local",
        "uid": "upcoming-1@example.invalid",
        "summary": "Dentist",
        "start": "2026-11-12T09:00:00-05:00",
        "end": "2026-11-12T09:30:00-05:00",
        "location": "",
        "minutes_until": 15,
    }


def test_free_slots_skip_busy_events_inside_working_hours(make_app: AppFactory) -> None:
    app = make_app(_config(_file_source(FIXTURES / "basic.ics")))
    outcome = app.call("calendar_free_slots", {**DAY, "duration_minutes": 60}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    assert outcome.data["slots"] == [
        {"start": "2026-11-12T09:00:00-05:00", "end": "2026-11-12T10:00:00-05:00", "minutes": 60},
        {"start": "2026-11-12T10:30:00-05:00", "end": "2026-11-12T12:00:00-05:00", "minutes": 90},
        {"start": "2026-11-12T13:30:00-05:00", "end": "2026-11-12T17:00:00-05:00", "minutes": 210},
    ]


def test_propose_without_a_mail_connector_writes_a_private_ics_file(
    make_app: AppFactory, data_dir: Path
) -> None:
    app = make_app(_config())
    outcome = app.call(
        "calendar_propose_event",
        {"summary": "Coffee", "start": "2026-11-20T10:00", "duration_minutes": 30},
        Origin.ASSISTANT,
    )
    assert outcome.status == "ok", outcome.message
    assert outcome.data["status"] == "file_saved"
    path = Path(outcome.data["path"])
    assert path.parent == data_dir / "calendar" / "outbox"
    assert path.name == f"{outcome.data['uid']}.ics"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert b"METHOD:REQUEST\r\n" in raw
    assert b"DTSTART:20261120T150000Z\r\n" in raw
    assert (outcome.data["start"], outcome.data["end"]) == (
        "2026-11-20T10:00:00-05:00",
        "2026-11-20T10:30:00-05:00",
    )


def test_invitations_need_owner_email(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["calendar"]}})
    outcome = app.call(
        "calendar_propose_event",
        {"summary": "Coffee", "start": "2026-11-20", "end": "2026-11-20"},
        Origin.ASSISTANT,
    )
    assert outcome.status == "error" and "owner_email" in outcome.message


def test_create_event_refuses_read_only_sources(make_app: AppFactory) -> None:
    app = make_app(_config(_file_source(FIXTURES / "basic.ics")))
    args = {"summary": "Gym", "start": "2026-11-20T07:00", "duration_minutes": 60, "source": "local"}
    outcome = app.call("calendar_create_event", args, Origin.HUMAN)
    assert outcome.status == "error" and "read-only" in outcome.message


def test_create_event_from_an_assistant_waits_for_approval(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_DAV_PASSWORD", "app-password")
    work = {
        "type": "caldav",
        "url": "https://dav.example.invalid/dav/calendars/owner/personal/",
        "username": "owner",
        "password": "env:SB_TEST_DAV_PASSWORD",
        "writable": True,
    }
    app = make_app(_config({"work": work}))
    args = {"summary": "Design review", "start": "2026-11-20T10:00", "duration_minutes": 45, "source": "work"}
    outcome = app.call("calendar_create_event", args, Origin.ASSISTANT)
    assert outcome.status == "pending"
    assert "create 'Design review' on 2026-11-20 10:00 America/New_York in work" in outcome.message
    assert fake_http.requests == []


def test_send_invite_runs_only_after_approval_through_mail_sender(make_app: AppFactory) -> None:
    app = make_app(_config(invite_account="personal"))
    mail = FakeMailService()
    app.ctx.provide("mail.sender", mail)
    args = {
        "summary": "Lunch",
        "start": "2026-11-20T12:00",
        "end": "2026-11-20T13:00",
        "attendees": ["sam@example.invalid"],
    }
    pending = app.call("calendar_send_invite", args, Origin.ASSISTANT)
    assert pending.status == "pending" and pending.approval_id is not None
    assert "to sam@example.invalid" in pending.message
    assert mail.sent == []

    done = app.dispatcher.approve(pending.approval_id)

    assert done.status == "ok", done.message
    [(account, message)] = mail.sent
    assert account == "personal"
    assert str(message["To"]) == "sam@example.invalid"
    calendar = next(part for part in message.walk() if part.get_content_type() == "text/calendar")
    assert "METHOD:REQUEST" in calendar.get_content()


def test_invite_attendees_are_checked_against_the_policy_allowlist(make_app: AppFactory) -> None:
    config = _config()
    config["policy"] = {"tools": {"calendar_send_invite": {"allow_recipients": ["*@example.invalid"]}}}
    app = make_app(config)
    args = {"summary": "Lunch", "start": "2026-11-20T12:00", "duration_minutes": 60}
    outcome = app.call(
        "calendar_send_invite", {**args, "attendees": ["someone@elsewhere.example"]}, Origin.ASSISTANT
    )
    assert outcome.status == "denied" and "someone@elsewhere.example" in outcome.message


def test_send_invite_without_the_mail_connector_explains_what_is_missing(make_app: AppFactory) -> None:
    app = make_app(_config())
    args = {
        "summary": "Lunch",
        "start": "2026-11-20T12:00",
        "duration_minutes": 60,
        "attendees": ["a@example.invalid"],
    }
    outcome = app.call("calendar_send_invite", args, Origin.HUMAN)
    assert outcome.status == "error" and "mail.sender" in outcome.message


def test_parse_moment_and_webcal_links() -> None:
    assert parse_moment("2026-11-12", NEW_YORK) == (datetime(2026, 11, 12, tzinfo=NEW_YORK), True)
    assert parse_moment("2026-11-12", NEW_YORK, end_of_day=True) == (
        datetime(2026, 11, 13, tzinfo=NEW_YORK),
        True,
    )
    assert parse_moment("2026-11-12T09:30", NEW_YORK) == (
        datetime(2026, 11, 12, 9, 30, tzinfo=NEW_YORK),
        False,
    )
    assert parse_moment("2026-11-12T14:30:00Z", NEW_YORK)[0] == datetime(2026, 11, 12, 14, 30, tzinfo=UTC)
    with pytest.raises(ToolError, match="ISO date"):
        parse_moment("2026-13-01", NEW_YORK)
    assert webcal_to_https("webcal://example.invalid/a.ics") == "https://example.invalid/a.ics"
    assert webcal_to_https("https://example.invalid/a.ics") == "https://example.invalid/a.ics"


def test_a_transient_feed_failure_is_retried_within_one_refresh(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpclient, "backoff_seconds", lambda _attempt: 0.0)
    fake_http.add("GET", FEED, status=503)
    app = _feed_app(make_app, fake_http, monkeypatch)

    state = _service(app).refresh("google", datetime.now(UTC))

    assert state["error"] is None and state["events"] == 5
    assert [request.url for request in fake_http.requests] == [FEED, FEED]
