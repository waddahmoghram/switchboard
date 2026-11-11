"""HTTP client safety: allowed schemes, redirect handling, and decoding. No network is used."""

from __future__ import annotations

import socket
import urllib.request
from collections.abc import Callable
from email.message import Message
from typing import Any

import pytest

from switchboard.errors import HttpError
from switchboard.httpclient import (
    HttpClient,
    HttpResponse,
    _NoRedirectHandler,
    _SafeRedirectHandler,
    basic_auth,
    check_url,
    require_public_host,
)


@pytest.mark.parametrize("url", ["https://example.com/x", "http://127.0.0.1:8123/api", "http://localhost/"])
def test_allowed_urls(url: str) -> None:
    check_url(url)


@pytest.mark.parametrize(
    "url", ["http://example.com/", "file:///etc/passwd", "ftp://example.com/", "example.com"]
)
def test_refused_urls(url: str) -> None:
    with pytest.raises(HttpError):
        check_url(url)


def test_plain_http_requires_an_explicit_opt_in() -> None:
    check_url("http://homeassistant.local:8123/", allow_http=True)


def test_basic_auth_header() -> None:
    assert basic_auth("user", "pass") == "Basic dXNlcjpwYXNz"


def _redirect(old: str, new: str) -> urllib.request.Request | None:
    request = urllib.request.Request(old, headers={"Authorization": "Bearer x", "Cookie": "c=1"})
    return _SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, new)


def test_redirects_drop_credentials_when_the_host_changes() -> None:
    redirected = _redirect("https://a.example.com/x", "https://b.example.com/y")
    assert redirected is not None
    assert not redirected.has_header("Authorization")
    assert not redirected.has_header("Cookie")


def test_redirects_keep_credentials_on_the_same_host() -> None:
    redirected = _redirect("https://a.example.com/x", "https://a.example.com/y")
    assert redirected is not None
    assert redirected.has_header("Authorization")


def test_redirects_never_downgrade_to_http() -> None:
    assert _redirect("https://a.example.com/x", "http://a.example.com/y") is None


def test_response_text_uses_the_declared_charset() -> None:
    body = "café".encode("latin-1")
    response = HttpResponse(
        200, "https://example.com", {"content-type": "text/plain; charset=iso-8859-1"}, body
    )
    assert response.ok
    assert response.text() == "café"


def test_the_no_redirect_handler_hands_3xx_back_to_the_caller() -> None:
    request = urllib.request.Request("https://a.example.com/x")
    assert (
        _NoRedirectHandler().redirect_request(request, None, 301, "Moved", {}, "https://b.example.com/")
        is None
    )


def test_unknown_charset_names_fall_back_to_utf8() -> None:
    response = HttpResponse(
        200, "https://example.com", {"content-type": "text/plain; charset=utf8mb4"}, b"ok"
    )
    assert response.text() == "ok"


def resolves_to(*addresses: str) -> Callable[..., list[tuple[Any, ...]]]:
    """A stand-in for socket.getaddrinfo: no DNS query leaves the machine."""

    def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    return getaddrinfo


def never(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("a connection was attempted")


class _Reply:
    status = 200
    headers = Message()

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"ok"

    def geturl(self) -> str:
        return "https://shop.example.com/"


@pytest.mark.parametrize("addresses", [("93.184.215.14",), ("2606:2800:21f:cb07:6820:80da:af6b:8b2c",)])
def test_public_addresses_pass(monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolves_to(*addresses))
    require_public_host("https://shop.example.com/p")


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.5",),
        ("192.168.1.20",),
        ("169.254.169.254",),  # cloud instance metadata
        ("100.64.0.1",),  # carrier-grade NAT
        ("::1",),
        ("fe80::1%eth0",),
        ("::ffff:127.0.0.1",),
        ("93.184.215.14", "10.0.0.5"),  # one private answer is enough
    ],
)
def test_names_resolving_to_non_public_addresses_are_refused(
    monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolves_to(*addresses))
    with pytest.raises(HttpError, match="non-public"):
        require_public_host("https://127.0.0.1.nip.io/p")


def test_unresolvable_names_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(HttpError, match="cannot resolve"):
        require_public_host("https://nowhere.example.invalid/")


def test_public_only_checks_the_address_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolves_to("127.0.0.1"))
    client = HttpClient()
    monkeypatch.setattr(client._opener, "open", never)
    monkeypatch.setattr(client._no_redirect_opener, "open", never)
    with pytest.raises(HttpError, match="non-public"):
        client.request("GET", "https://shop.example.com/", public_only=True)


def test_public_only_never_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolves_to("93.184.215.14"))
    client = HttpClient()
    monkeypatch.setattr(client._opener, "open", never)
    monkeypatch.setattr(client._no_redirect_opener, "open", lambda *_args, **_kwargs: _Reply())

    response = client.request("GET", "https://shop.example.com/", public_only=True)

    assert (response.status, response.body) == (200, b"ok")
