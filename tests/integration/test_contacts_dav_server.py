"""Integration tests: the contacts connector against a local fake CardDAV server over real HTTP.

Discovery starts at the server root, which is missing, so the client follows RFC 6764: the
``/.well-known/carddav`` redirect, the principal, the address book home, and finally a multiget.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App, build_app
from switchboard.config import config_from_dict
from switchboard.httpclient import HttpClient
from switchboard.policy import Origin

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
USERNAME = "owner"
PASSWORD = "app-password-for-tests"
AUTHORIZATION = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode("ascii")
BOOK = "/dav/addressbooks/owner/default/"


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


class FakeCardDavServer(ThreadingHTTPServer):
    """Canned replies keyed by (method, path); every request is recorded."""

    def __init__(self, routes: dict[tuple[str, str], Reply]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes = routes
        self.seen: list[Seen] = []

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeCardDavServer

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.server.seen.append(Seen(self.command, self.path, headers, body))
        if headers.get("authorization") != AUTHORIZATION:
            reply = Reply(401)
        else:
            reply = self.server.routes.get((self.command, self.path), Reply(404))
        self.send_response(reply.status)
        for name, value in {"Content-Type": "application/xml; charset=utf-8", **reply.headers}.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    do_PROPFIND = do_REPORT = _serve

    def log_message(self, format: str, *args: Any) -> None:
        return  # keep test output quiet


def _xml(name: str) -> Reply:
    return Reply(207, (FIXTURES / name).read_bytes())


@pytest.fixture
def carddav_server() -> Iterator[FakeCardDavServer]:
    server = FakeCardDavServer(
        {
            ("PROPFIND", "/.well-known/carddav"): Reply(301, headers={"Location": "/dav/"}),
            ("PROPFIND", "/dav/"): _xml("principal.xml"),
            ("PROPFIND", "/dav/principals/owner/"): _xml("home_set.xml"),
            ("PROPFIND", "/dav/addressbooks/owner/"): _xml("propfind_home.xml"),
            ("PROPFIND", BOOK): _xml("propfind_cards.xml"),
            ("REPORT", BOOK): _xml("multiget.xml"),
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
def contacts_app(carddav_server: FakeCardDavServer, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    monkeypatch.setenv("SB_TEST_CARDDAV_PASSWORD", PASSWORD)
    source = {
        "type": "carddav",
        "url": carddav_server.url + "/",
        "username": USERNAME,
        "password": "env:SB_TEST_CARDDAV_PASSWORD",
    }
    config = {
        "switchboard": {"connectors": ["contacts"]},
        "connectors": {"contacts": {"sources": {"icloud": source}}},
    }
    return build_app(config_from_dict(config, data_dir), http=HttpClient(timeout=5))


def test_search_discovers_the_address_book_and_downloads_cards(
    contacts_app: App, carddav_server: FakeCardDavServer
) -> None:
    outcome = contacts_app.call("contacts_search", {"query": "rivera"}, Origin.ASSISTANT)

    assert outcome.status == "ok", outcome.message
    assert outcome.data["warnings"] == []
    [alex] = outcome.data["results"]
    assert alex["name"] == "Dr. Alex J. Rivera"
    assert alex["emails"] == [{"address": "alex.rivera@example.invalid", "types": ["work"]}]
    assert [(seen.method, seen.path, seen.headers.get("depth")) for seen in carddav_server.seen] == [
        ("PROPFIND", "/", "0"),
        ("PROPFIND", "/.well-known/carddav", "0"),
        ("PROPFIND", "/dav/", "0"),
        ("PROPFIND", "/dav/principals/owner/", "0"),
        ("PROPFIND", "/dav/addressbooks/owner/", "1"),
        ("PROPFIND", BOOK, "1"),
        ("REPORT", BOOK, "0"),
    ]
    multiget = carddav_server.seen[-1].body.decode("utf-8")
    assert "<CR:addressbook-multiget" in multiget
    assert f"<D:href>{BOOK}alex.vcf</D:href>" in multiget
    assert f"<D:href>{BOOK}sam.vcf</D:href>" in multiget


def test_cached_cards_answer_later_searches_without_network(
    contacts_app: App, carddav_server: FakeCardDavServer
) -> None:
    assert contacts_app.call("contacts_search", {"query": "rivera"}, Origin.ASSISTANT).status == "ok"
    requests = len(carddav_server.seen)

    by_phone = contacts_app.call("contacts_search", {"query": "+44 20 7946"}, Origin.ASSISTANT)

    assert [result["name"] for result in by_phone.data["results"]] == ["Sam Example"]
    assert len(carddav_server.seen) == requests
    [source] = contacts_app.call("contacts_sources", {}, Origin.ASSISTANT).data["sources"]
    assert (source["name"], source["type"], source["cards"], source["error"]) == (
        "icloud",
        "carddav",
        2,
        None,
    )
