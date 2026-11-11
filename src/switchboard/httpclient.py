"""A small, safe HTTP client on top of :mod:`urllib`.

Safety defaults:

* HTTPS only, except loopback addresses, unless a caller passes ``allow_http=True`` (for a
  Home Assistant or ntfy server on your own network).
* Redirects never downgrade from HTTPS to HTTP, and ``Authorization`` and ``Cookie`` headers are
  dropped when a redirect changes host (urllib forwards them by default).
* Responses are size-capped, with per-socket timeouts rather than a whole-request deadline.
* Non-2xx statuses are returned, not raised, so connectors can read error bodies; network failures
  raise :class:`~switchboard.errors.HttpError`.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from typing import Any, Protocol

from switchboard import __version__
from switchboard.errors import HttpError
from switchboard.util import json_loads

DEFAULT_USER_AGENT = f"switchboard/{__version__} (self-hosted personal assistant hooks)"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        message = Message()
        message["content-type"] = self.headers.get("content-type", "text/plain")
        charset = message.get_content_charset() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:  # an unknown charset label such as "utf8mb4"
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json_loads(self.body)


class HttpTransport(Protocol):
    """What connectors depend on; tests pass a fake with the same method."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        allow_http: bool = False,
        follow_redirects: bool = True,
        public_only: bool = False,
    ) -> HttpResponse: ...


def basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def check_url(url: str, *, allow_http: bool = False) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (allow_http or (parts.hostname or "") in _LOOPBACK):
        return
    raise HttpError(f"refusing {parts.scheme or 'schemeless'} URL for host {parts.hostname!r}; use HTTPS")


AddressInfo = tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]]


def _public_addresses(host: str, port: int) -> Sequence[AddressInfo]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as error:
        raise HttpError(f"cannot resolve host {host!r}") from error
    if any(info[0] not in {socket.AF_INET, socket.AF_INET6} for info in infos):
        raise HttpError(f"refusing host {host!r}: unsupported address family")
    addresses = {str(info[4][0]) for info in infos}
    if not addresses or not all(_is_public(address) for address in addresses):
        raise HttpError(f"refusing host {host!r}: it resolves to a non-public address")
    return infos


def require_public_host(url: str) -> None:
    """Check all resolved addresses. Connections must separately pin these addresses, not resolve again."""
    parts = urllib.parse.urlsplit(url)
    _public_addresses(parts.hostname or "", parts.port or 443)


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback, whatever the Python version says
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    _tunnel_host: str | None
    source_address: tuple[str, int] | None
    _context: ssl.SSLContext

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise HttpError("public-only requests cannot use a proxy tunnel")
        addresses = _public_addresses(self.host, self.port)
        timeout = self.timeout
        if timeout is not None and not isinstance(timeout, (int, float)):
            timeout = socket.getdefaulttimeout()
        last_error: OSError | None = None
        for family, kind, proto, _canonname, address in addresses:
            raw = socket.socket(family, kind, proto)
            try:
                raw.settimeout(timeout)
                if self.source_address is not None:
                    raw.bind(self.source_address)
                raw.connect(address)
            except OSError as exc:
                raw.close()
                last_error = exc
                continue
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except BaseException:
                raw.close()
                raise
            return
        raise HttpError(f"could not connect to public host {self.host!r}") from last_error


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    _context: ssl.SSLContext | None

    def https_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if old.scheme == "https" and new.scheme != "https":
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (old.hostname, old.port) != (new.hostname, new.port):
            for name in ("Authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses to the caller instead of following them."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        max_bytes: int = 5_000_000,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())
        self._no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())
        self._public_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _PublicHTTPSHandler(), _NoRedirectHandler()
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        allow_http: bool = False,
        follow_redirects: bool = True,
        public_only: bool = False,
    ) -> HttpResponse:
        """Public-only requests pin validated addresses, bypass proxies, and return redirects for vetting."""
        check_url(url, allow_http=allow_http)
        if public_only and urllib.parse.urlsplit(url).scheme != "https":
            raise HttpError("public-only requests require HTTPS")
        limit = max_bytes or self.max_bytes
        merged = {"User-Agent": self.user_agent, **dict(headers or {})}
        # check_url above restricts schemes to https (and http for loopback or opted-in hosts).
        request = urllib.request.Request(url, data=body, method=method.upper(), headers=merged)  # noqa: S310
        try:
            opener = self._opener if follow_redirects else self._no_redirect_opener
            if public_only:
                opener = self._public_opener
            with opener.open(request, timeout=timeout or self.timeout) as response:
                payload = response.read(limit + 1)
                status = response.status
                final_url = response.geturl()
                raw_headers = response.headers.items()
        except urllib.error.HTTPError as error:
            payload = error.read(limit + 1) if error.fp is not None else b""
            status = error.code
            final_url = error.geturl() or url
            raw_headers = error.headers.items() if error.headers is not None else []
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise HttpError(f"{method.upper()} {urllib.parse.urlsplit(url).hostname}: {reason}") from error
        if len(payload) > limit:
            raise HttpError(f"response from {urllib.parse.urlsplit(url).hostname} exceeded {limit} bytes")
        lowered = {key.lower(): value for key, value in raw_headers}
        return HttpResponse(status=status, url=final_url, headers=lowered, body=payload)

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post_json(
        self, url: str, payload: Any, *, headers: Mapping[str, str] | None = None, **kwargs: Any
    ) -> HttpResponse:
        merged = {"Content-Type": "application/json", **dict(headers or {})}
        body = json.dumps(payload).encode("utf-8")
        return self.request("POST", url, headers=merged, body=body, **kwargs)
