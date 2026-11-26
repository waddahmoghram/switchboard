"""The calendar connector's doctor: settings, secret references, files, and each source's last refresh.

The doctor must report problems instead of raising, never contact a server, and never show a secret,
including the private feed address.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors import calendar
from switchboard.connectors.calendar.service import CalendarService
from switchboard.connectors.calendar.settings import load_settings
from switchboard.errors import ConfigError
from switchboard.httpclient import HttpResponse
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
PRIVATE = "private-0000"
FEED = f"https://calendar.example.invalid/{PRIVATE}/basic.ics"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CALDAV = {
    "type": "caldav",
    "url": "https://dav.example.invalid/",
    "username": "owner",
    "password": "env:SB_TEST_CALDAV_PASSWORD",
}
WINDOW_WARNING = (
    "upcoming_minutes is shorter than refresh_interval_seconds, so some events would never be announced"
)


def _config(sources: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    table = {"sources": sources or {}, **extra}
    return {"switchboard": {"connectors": ["calendar"]}, "connectors": {"calendar": table}}


def test_a_healthy_configuration_has_no_problems_and_calls_no_server(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SB_TEST_CALENDAR_URL", FEED)
    monkeypatch.setenv("SB_TEST_CALDAV_PASSWORD", "caldav-password-0000")
    (tmp_path / "home.ics").write_bytes((FIXTURES / "basic.ics").read_bytes())
    sources = {
        "feed": {"type": "ics_url", "url": "env:SB_TEST_CALENDAR_URL"},
        "dav": CALDAV,
        "local": {"type": "ics_file", "path": "~/home.ics"},
    }
    app = make_app(_config(sources))
    monkeypatch.setenv("HOME", str(tmp_path))  # the file is found the way refresh reads it, after ~

    assert calendar.doctor(app.ctx) == []
    assert fake_http.requests == []


def test_each_source_names_its_unresolvable_reference_or_missing_file(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SB_TEST_UNSET_CALENDAR_URL", raising=False)
    monkeypatch.delenv("SB_TEST_CALDAV_PASSWORD", raising=False)
    sources = {
        "feed": {"type": "ics_url", "url": "env:SB_TEST_UNSET_CALENDAR_URL"},
        "dav": CALDAV,
        "gone": {"type": "ics_file", "path": str(tmp_path / "missing.ics")},
        "folder": {"type": "ics_file", "path": str(tmp_path)},
    }
    app = make_app(_config(sources))

    assert calendar.doctor(app.ctx) == [
        "source 'feed': url: environment variable 'SB_TEST_UNSET_CALENDAR_URL' is not set",
        "source 'dav': password: environment variable 'SB_TEST_CALDAV_PASSWORD' is not set",
        "source 'gone': path does not point to a file",
        "source 'folder': path does not point to a file",
    ]


def test_no_sources_is_a_problem(make_app: AppFactory) -> None:
    app = make_app(_config())
    assert calendar.doctor(app.ctx) == ["no sources are configured under [connectors.calendar.sources]"]


@pytest.mark.parametrize(
    ("upcoming_minutes", "refresh_seconds", "warned"),
    [(15, 900, False), (14, 900, True), (1, 60, False), (60, 3601, True)],
)
def test_the_upcoming_window_must_cover_one_refresh_interval(
    make_app: AppFactory, upcoming_minutes: int, refresh_seconds: int, warned: bool
) -> None:
    app = make_app(
        _config(
            {"local": {"type": "ics_file", "path": str(FIXTURES / "basic.ics")}},
            upcoming_minutes=upcoming_minutes,
            refresh_interval_seconds=refresh_seconds,
        )
    )
    assert calendar.doctor(app.ctx) == ([WINDOW_WARNING] if warned else [])


def test_the_last_refresh_failure_is_shown_without_the_feed_address_until_a_refresh_succeeds(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_CALENDAR_URL", f"webcal://calendar.example.invalid/{PRIVATE}/basic.ics")
    app = make_app(_config({"google": {"type": "ics_url", "url": "env:SB_TEST_CALENDAR_URL"}}))
    service = CalendarService(app.ctx, load_settings(app.ctx.connector_config("calendar")))

    fake_http.add("GET", FEED, status=404)
    service.refresh("google", NOW)
    assert calendar.doctor(app.ctx) == [
        "source 'google': last refresh failed: calendar source 'google': the feed answered HTTP 404"
    ]

    def reset(request: RecordedRequest) -> HttpResponse:
        raise ConnectionResetError(f"connection to {request.url} was reset")

    fake_http.add_handler("GET", FEED, reset, repeat=False)
    service.refresh("google", NOW + timedelta(minutes=15))
    problems = calendar.doctor(app.ctx)
    assert problems == ["source 'google': last refresh failed: unexpected ConnectionResetError; see the log"]
    assert PRIVATE not in "\n".join(problems)

    fake_http.add("GET", FEED, body=(FIXTURES / "basic.ics").read_bytes())
    service.refresh("google", NOW + timedelta(minutes=30))
    assert calendar.doctor(app.ctx) == []


@pytest.mark.parametrize(
    "server",
    [
        {"url": "http://127.0.0.1:5232/"},
        {"url": "http://localhost:5232/"},
        {"url": "http://dav.home.invalid/", "allow_http": True},
    ],
)
def test_plain_http_caldav_is_accepted_on_loopback_or_with_allow_http(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, server: dict[str, Any]
) -> None:
    monkeypatch.setenv("SB_TEST_CALDAV_PASSWORD", "caldav-password-0000")
    app = make_app(_config({"dav": {**CALDAV, **server}}))
    assert calendar.doctor(app.ctx) == []


@pytest.mark.parametrize(
    ("table", "message"),
    [
        (
            {"sources": {"bad name": {"type": "ics_file", "path": "home.ics"}}},
            "source names may use letters",
        ),
        ({"sources": {"home": FEED}}, "[connectors.calendar.sources.home] must be a table"),
        ({"sources": ["home"]}, "[connectors.calendar.sources] must be a table of named sources"),
        ({"owner_email": "Owner <owner@example.invalid>"}, "owner_email must be a plain email address"),
        ({"sources": {"dav": {**CALDAV, "allow_http": "yes"}}}, "allow_http must be true or false"),
        ({"refresh_interval_seconds": 30}, "refresh_interval_seconds must be a whole number from 60"),
        ({"timezone": "Mars/Olympus_Mons"}, "is not a known IANA time zone"),
    ],
)
def test_doctor_reports_an_invalid_table_as_start_up_would(
    make_app: AppFactory, table: dict[str, Any], message: str
) -> None:
    problems = calendar.doctor(make_app({"connectors": {"calendar": table}}).ctx)

    with pytest.raises(ConfigError) as raised:
        make_app({"switchboard": {"connectors": ["calendar"]}, "connectors": {"calendar": table}})
    assert problems == [str(raised.value)]
    assert message in problems[0]
