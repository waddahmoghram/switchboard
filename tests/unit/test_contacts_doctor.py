"""The contacts connector's doctor: settings, the CardDAV password reference, files, and refresh errors.

The doctor must report problems instead of raising, never contact a server, and never show a secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors import contacts
from switchboard.connectors.contacts.service import ContactsService
from switchboard.connectors.contacts.settings import load_settings
from switchboard.errors import ConfigError
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CARDDAV = {
    "type": "carddav",
    "url": "https://dav.example.invalid/",
    "username": "owner",
    "password": "env:SB_TEST_CARDDAV_PASSWORD",
}


def _config(sources: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    table = {"sources": sources or {}, **extra}
    return {"switchboard": {"connectors": ["contacts"]}, "connectors": {"contacts": table}}


def test_a_healthy_configuration_has_no_problems_and_calls_no_server(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SB_TEST_CARDDAV_PASSWORD", "carddav-password-0000")
    (tmp_path / "phone.vcf").write_bytes((FIXTURES / "contacts.vcf").read_bytes())
    app = make_app(_config({"dav": CARDDAV, "phone": {"type": "vcf_file", "path": "~/phone.vcf"}}))
    monkeypatch.setenv("HOME", str(tmp_path))  # the file is found the way refresh reads it, after ~

    assert contacts.doctor(app.ctx) == []
    assert fake_http.requests == []


def test_each_source_names_its_unresolvable_password_or_missing_file(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SB_TEST_CARDDAV_PASSWORD", raising=False)
    sources = {
        "dav": CARDDAV,
        "gone": {"type": "vcf_file", "path": str(tmp_path / "missing.vcf")},
        "folder": {"type": "vcf_file", "path": str(tmp_path)},
    }
    app = make_app(_config(sources))

    assert contacts.doctor(app.ctx) == [
        "source 'dav': password: environment variable 'SB_TEST_CARDDAV_PASSWORD' is not set",
        "source 'gone': path does not point to a file",
        "source 'folder': path does not point to a file",
    ]


def test_no_sources_is_a_problem(make_app: AppFactory) -> None:
    app = make_app(_config())
    assert contacts.doctor(app.ctx) == ["no sources are configured under [connectors.contacts.sources]"]


def test_the_last_refresh_failure_is_shown_until_a_refresh_succeeds(
    make_app: AppFactory, tmp_path: Path
) -> None:
    path = tmp_path / "phone.vcf"
    app = make_app(_config({"phone": {"type": "vcf_file", "path": str(path)}}))
    service = ContactsService(app.ctx, load_settings(app.ctx.connector_config("contacts")))

    service.refresh("phone", NOW)
    assert contacts.doctor(app.ctx) == [
        "source 'phone': path does not point to a file",
        "source 'phone': last refresh failed: "
        "contacts source 'phone': cannot read the file (No such file or directory)",
    ]

    path.write_bytes((FIXTURES / "contacts.vcf").read_bytes())
    service.refresh("phone", NOW + timedelta(hours=6))
    assert contacts.doctor(app.ctx) == []


@pytest.mark.parametrize(
    "server",
    [
        {"url": "http://127.0.0.1:5232/"},
        {"url": "http://localhost:5232/"},
        {"url": "http://dav.home.invalid/", "allow_http": True},
    ],
)
def test_plain_http_carddav_is_accepted_on_loopback_or_with_allow_http(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, server: dict[str, Any]
) -> None:
    monkeypatch.setenv("SB_TEST_CARDDAV_PASSWORD", "carddav-password-0000")
    app = make_app(_config({"dav": {**CARDDAV, **server}}))
    assert contacts.doctor(app.ctx) == []


@pytest.mark.parametrize(
    ("table", "message"),
    [
        (
            {"sources": {"bad name": {"type": "vcf_file", "path": "phone.vcf"}}},
            "source names may use letters",
        ),
        ({"sources": {"phone": "phone.vcf"}}, "[connectors.contacts.sources.phone] must be a table"),
        ({"sources": ["phone"]}, "[connectors.contacts.sources] must be a table of named sources"),
        (
            {"sources": {"dav": {**CARDDAV, "url": "http://dav.example.invalid/"}}},
            "url must start with https://",
        ),
        ({"sources": {"dav": {**CARDDAV, "allow_http": 1}}}, "allow_http must be true or false"),
        ({"refresh_interval_seconds": 299}, "refresh_interval_seconds must be a whole number from 300"),
        ({"refresh_interval_seconds": True}, "refresh_interval_seconds must be a whole number from 300"),
    ],
)
def test_doctor_reports_an_invalid_table_as_start_up_would(
    make_app: AppFactory, table: dict[str, Any], message: str
) -> None:
    problems = contacts.doctor(make_app({"connectors": {"contacts": table}}).ctx)

    with pytest.raises(ConfigError) as raised:
        make_app({"switchboard": {"connectors": ["contacts"]}, "connectors": {"contacts": table}})
    assert problems == [str(raised.value)]
    assert message in problems[0]
