"""Unit tests for the shared WebDAV helper: multistatus parsing, XML safety, redirects, and errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.connectors import _dav
from tests.fakes import FakeHttp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
BASE = "https://dav.example.invalid/dav/calendars/owner/"
CALENDAR = _dav.qname(_dav.CALDAV, "calendar")


def _client(http: FakeHttp, url: str = BASE) -> _dav.DavClient:
    return _dav.DavClient(http, url, "owner@example.invalid", "app-password", "test source")


def test_multistatus_resolves_hrefs_and_keeps_only_successful_properties() -> None:
    resources = _dav.parse_multistatus((FIXTURES / "propfind_home.xml").read_bytes(), BASE)
    assert [resource.href for resource in resources] == [
        BASE,
        BASE + "personal/",
        BASE + "tasks/",
        BASE + "gone/",
    ]
    home, personal, _tasks, gone = resources
    assert home.types() == {_dav.COLLECTION}
    assert personal.types() == {_dav.COLLECTION, CALENDAR}
    assert personal.text(_dav.DISPLAYNAME) == "Personal"
    assert _dav.qname(_dav.CALDAV, "calendar-description") not in personal.props
    assert gone.status == 404
    assert gone.props == {}


def test_link_properties_resolve_against_the_request_url() -> None:
    [resource] = _dav.parse_multistatus(
        (FIXTURES / "home_set.xml").read_bytes(), "https://dav.example.invalid/x/"
    )
    assert resource.link(_dav.qname(_dav.CALDAV, "calendar-home-set")) == (
        "https://dav.example.invalid/dav/calendars/owner/"
    )
    assert resource.link(_dav.CURRENT_USER_PRINCIPAL) is None


def test_doctype_is_refused_before_parsing() -> None:
    body = (
        b'<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol">]>\n'
        b'<d:multistatus xmlns:d="DAV:">&lol;</d:multistatus>'
    )
    with pytest.raises(_dav.DavError, match="DOCTYPE"):
        _dav.parse_multistatus(body, BASE)


@pytest.mark.parametrize("hider", [b"<!--<a-->", b"<?pi <a?>", b"\n<?pi x?>\n<!-- <c -->\n"])
def test_doctype_behind_a_comment_or_processing_instruction_is_still_refused(hider: bytes) -> None:
    body = (
        b'<?xml version="1.0"?>' + hider + b'<!DOCTYPE m [<!ENTITY z "zz">]>'
        b'<d:multistatus xmlns:d="DAV:">&z;</d:multistatus>'
    )
    with pytest.raises(_dav.DavError, match="DOCTYPE"):
        _dav.parse_xml(body)


def test_doctype_text_inside_cdata_is_ordinary_content() -> None:
    body = (
        b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:response><d:href>/a.ics</d:href><d:propstat><d:prop><c:calendar-data>"
        b"<![CDATA[X-ALT-DESC;FMTTYPE=text/html:<!DOCTYPE html>]]></c:calendar-data></d:prop>"
        b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    )
    [resource] = _dav.parse_multistatus(body, BASE)
    assert "<!DOCTYPE html>" in resource.text(_dav.qname(_dav.CALDAV, "calendar-data"))


@pytest.mark.parametrize(
    "body",
    [
        b"<d:multistatus xmlns:d='DAV:'><d:response>",
        b"\xff\xfe<\x00a\x00/\x00>\x00",
        b"<d:other xmlns:d='DAV:'/>",
    ],
)
def test_malformed_replies_raise_dav_errors(body: bytes) -> None:
    with pytest.raises(_dav.DavError):
        _dav.parse_multistatus(body, BASE)


@pytest.mark.parametrize(
    ("configured", "candidate", "expected"),
    [
        ("caldav.icloud.com", "p42-caldav.icloud.com", True),
        ("caldav.icloud.com", "caldav.icloud.com", True),
        ("contacts.icloud.com", "p42-contacts.icloud.com", True),
        ("p42-contacts.icloud.com", "p43-contacts.icloud.com", True),
        ("caldav.icloud.com", "icloud.com.example", False),
        ("caldav.icloud.com", "unrelated.icloud.com", False),
        ("caldav.icloud.com", "p42-contacts.icloud.com", False),
        ("dav.example.com", "other.example.com", False),
        ("calendar.example.co.uk", "attacker.co.uk", False),
        ("example.com", "other.example.com", False),
        ("10.0.0.5", "1.0.0.5", False),
    ],
)
def test_trusted_host(configured: str, candidate: str, expected: bool) -> None:
    assert _dav.trusted_host(configured, candidate) is expected


def test_propfind_sends_basic_auth_depth_and_a_propfind_body(fake_http: FakeHttp) -> None:
    fake_http.add("PROPFIND", BASE, status=207, body=(FIXTURES / "propfind_home.xml").read_bytes())
    resources = _client(fake_http).propfind(BASE, [_dav.RESOURCETYPE, _dav.DISPLAYNAME], depth="1")
    assert len(resources) == 4
    [request] = fake_http.requests
    assert request.headers["authorization"].startswith("Basic ")
    assert request.headers["depth"] == "1"
    assert request.body is not None
    assert b"<D:propfind" in request.body and b"<D:resourcetype/>" in request.body


def test_same_origin_redirects_are_followed(fake_http: FakeHttp) -> None:
    fake_http.add("PROPFIND", BASE, status=301, headers={"Location": "/dav/moved/"})
    fake_http.add(
        "PROPFIND",
        "https://dav.example.invalid/dav/moved/",
        status=207,
        body=(FIXTURES / "home_set.xml").read_bytes(),
    )
    [resource] = _client(fake_http).propfind(BASE, [_dav.DISPLAYNAME])
    assert resource.base == "https://dav.example.invalid/dav/moved/"
    assert [request.url for request in fake_http.requests] == [BASE, "https://dav.example.invalid/dav/moved/"]
    assert all(not request.follow_redirects for request in fake_http.requests)


def test_redirect_to_an_untrusted_host_is_refused(fake_http: FakeHttp) -> None:
    fake_http.add("PROPFIND", BASE, status=302, headers={"Location": "https://collector.example/steal/"})
    with pytest.raises(_dav.DavError, match="refusing to send credentials"):
        _client(fake_http).propfind(BASE, [_dav.DISPLAYNAME])
    assert len(fake_http.requests) == 1


def test_credentials_do_not_cross_ports_on_the_same_host(fake_http: FakeHttp) -> None:
    with pytest.raises(_dav.DavError, match="refusing to send credentials"):
        _client(fake_http).get("https://dav.example.invalid:8443/")
    assert fake_http.requests == []


def test_get_redirects_are_checked_before_the_next_request(fake_http: FakeHttp) -> None:
    fake_http.add("GET", BASE, status=302, headers={"Location": "https://other.example.invalid/"})
    with pytest.raises(_dav.DavError, match="refusing to send credentials"):
        _client(fake_http).get(BASE)
    assert len(fake_http.requests) == 1
    assert not fake_http.requests[0].follow_redirects


@pytest.mark.parametrize(
    ("status", "phrase"),
    [(401, "app-specific password"), (403, "HTTP 403"), (404, "HTTP 404"), (500, "HTTP 500")],
)
def test_error_statuses_have_clear_messages(fake_http: FakeHttp, status: int, phrase: str) -> None:
    fake_http.add("PROPFIND", BASE, status=status)
    with pytest.raises(_dav.DavError, match=phrase) as caught:
        _client(fake_http).propfind(BASE, [_dav.DISPLAYNAME])
    assert caught.value.status == status
    assert "app-password" not in str(caught.value)


def test_put_creates_only_and_reports_conflicts(fake_http: FakeHttp) -> None:
    target = BASE + "personal/new.ics"
    fake_http.add("PUT", target, status=201)
    fake_http.add("PUT", target, status=412)
    client = _client(fake_http)
    client.put(target, b"BEGIN:VCALENDAR", "text/calendar; charset=utf-8")
    assert fake_http.requests[0].headers["if-none-match"] == "*"
    assert fake_http.requests[0].headers["content-type"] == "text/calendar; charset=utf-8"
    with pytest.raises(_dav.DavError, match="already exists"):
        client.put(target, b"BEGIN:VCALENDAR", "text/calendar; charset=utf-8")


def test_password_is_not_part_of_the_client_repr(fake_http: FakeHttp) -> None:
    assert "app-password" not in repr(_client(fake_http))


def test_throttled_replies_carry_retry_after(fake_http: FakeHttp) -> None:
    fake_http.add("PROPFIND", BASE, status=503, headers={"Retry-After": "120"})
    with pytest.raises(_dav.DavError, match="busy") as caught:
        _client(fake_http).propfind(BASE, [_dav.DISPLAYNAME])
    assert (caught.value.status, caught.value.retry_after) == (503, 120.0)
