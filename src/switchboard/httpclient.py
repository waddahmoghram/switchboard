"""A small, safe HTTP client on top of :mod:`urllib`.

Safety defaults:

* HTTPS only, except loopback addresses, unless a caller passes ``allow_http=True`` (for a
  Home Assistant or ntfy server on your own network).
* Redirects never downgrade from HTTPS to HTTP, and ``Authorization`` and ``Cookie`` headers are
  dropped when a redirect changes host (urllib forwards them by default).
* Responses are size-capped, with per-socket timeouts. Inside a tool execution or a polling job, the
  deadline from :mod:`switchboard.execution` also applies: the socket timeout is clamped to the time
  left, nothing is sent once the deadline has passed, and a response still arriving after it fails.
* Non-2xx statuses are returned, not raised, so connectors can read error bodies; network failures
  raise :class:`~switchboard.errors.HttpError` (:class:`TransientHttpError` when repeating might help).
* Repeating a request is opt-in and only for GET and HEAD: see :func:`request_with_retries`.

What the deadline does not bound: name resolution on the default path (urllib asks the system resolver,
which has its own timeouts; only public-only requests resolve through a bounded helper), and a single
receive call, which can still wait up to the per-request timeout before the deadline is checked again.

One client is safe to share between threads: every request opens its own connection, and the openers'
handlers keep no per-request state.
"""

from __future__ import annotations

import base64
import email.utils
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from typing import Any, Protocol

from switchboard import __version__, execution
from switchboard.errors import HttpError
from switchboard.util import json_loads, now

DEFAULT_USER_AGENT = f"switchboard/{__version__} (self-hosted personal assistant hooks)"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})
RETRY_STATUSES = frozenset({408, 429, 502, 503, 504})
MAX_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 30.0
RESOLVE_TIMEOUT_SECONDS = 20.0
_MIN_ATTEMPT_SECONDS = 1.0
_READ_CHUNK = 65_536
_RESOLVER_SLOTS = threading.BoundedSemaphore(8)
_JITTER = secrets.SystemRandom()


class TransientHttpError(HttpError):
    """A network failure that repeating the request might avoid: a refused or reset connection, a socket
    timeout, an interrupted response, or a name lookup that did not finish in time."""


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


def _resolve(host: str, port: int, timeout: float | None) -> list[AddressInfo]:
    """``getaddrinfo`` that stops waiting after ``timeout`` seconds; a stalled lookup ends on its own."""
    if timeout is None:
        return list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise TransientHttpError(f"too many slow name lookups are pending; not resolving {host!r}")
    found: list[AddressInfo] = []
    failed: list[BaseException] = []
    done = threading.Event()

    def lookup() -> None:
        try:
            found.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            failed.append(exc)
        finally:
            _RESOLVER_SLOTS.release()
            done.set()

    threading.Thread(target=lookup, name="switchboard-resolve", daemon=True).start()
    if not done.wait(max(0.0, timeout)):
        raise TransientHttpError(f"resolving host {host!r} took longer than {timeout:.3g} seconds")
    if failed:
        raise failed[0]
    return found


def _public_addresses(host: str, port: int, timeout: float | None = None) -> Sequence[AddressInfo]:
    try:
        infos = _resolve(host, port, timeout)
    except (OSError, UnicodeError, ValueError) as error:
        raise HttpError(f"cannot resolve host {host!r}") from error
    if any(info[0] not in {socket.AF_INET, socket.AF_INET6} for info in infos):
        raise HttpError(f"refusing host {host!r}: unsupported address family")
    addresses = {str(info[4][0]) for info in infos}
    if not addresses or not all(_is_public(address) for address in addresses):
        raise HttpError(f"refusing host {host!r}: it resolves to a non-public address")
    return infos


def require_public_host(url: str) -> None:
    """Check all resolved addresses. Connections must separately pin these addresses, not resolve again.

    The lookup waits at most :data:`RESOLVE_TIMEOUT_SECONDS`, or less when a deadline is closer.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    left = _time_left("resolve", host)
    wait = RESOLVE_TIMEOUT_SECONDS if left is None else min(left, RESOLVE_TIMEOUT_SECONDS)
    _public_addresses(host, parts.port or 443, wait)


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
        timeout = self.timeout
        if timeout is not None and not isinstance(timeout, (int, float)):
            timeout = socket.getdefaulttimeout()
        addresses = _public_addresses(self.host, self.port, timeout)
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
        raise TransientHttpError(f"could not connect to public host {self.host!r}") from last_error


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
        retries: int = 0,
    ) -> HttpResponse:
        """Public-only requests pin validated addresses, bypass proxies, and return redirects for vetting.

        ``retries`` (0 to 3, GET and HEAD only) repeats transient failures; see :func:`request_with_retries`.
        """
        check_retries(method, retries)
        if retries:
            if body is not None:
                raise ValueError("a retried request cannot carry a body")
            return request_with_retries(
                self,
                method,
                url,
                retries=retries,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                allow_http=allow_http,
                follow_redirects=follow_redirects,
                public_only=public_only,
            )
        check_url(url, allow_http=allow_http)
        if public_only and urllib.parse.urlsplit(url).scheme != "https":
            raise HttpError("public-only requests require HTTPS")
        verb = method.upper()
        host = urllib.parse.urlsplit(url).hostname
        limit = max_bytes or self.max_bytes
        wait = timeout or self.timeout
        left = _time_left(verb, host)
        if left is not None:
            wait = min(wait, left)
        merged = {"User-Agent": self.user_agent, **dict(headers or {})}
        # check_url above restricts schemes to https (and http for loopback or opted-in hosts).
        request = urllib.request.Request(url, data=body, method=verb, headers=merged)  # noqa: S310
        try:
            opener = self._opener if follow_redirects else self._no_redirect_opener
            if public_only:
                opener = self._public_opener
            with opener.open(request, timeout=wait) as response:
                _check_late(verb, host)
                payload = _read_body(response, limit, verb, host)
                status = response.status
                final_url = response.geturl()
                raw_headers = response.headers.items()
        except urllib.error.HTTPError as error:
            _check_late(verb, host)
            status = error.code
            final_url = error.geturl() or url
            raw_headers = error.headers.items() if error.headers is not None else []
            try:
                payload = _read_body(error, limit, verb, host) if error.fp is not None else b""
            except (OSError, http.client.HTTPException) as failure:
                raise TransientHttpError(f"{verb} {host}: {failure}") from failure
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            _check_late(verb, host)
            reason = getattr(error, "reason", error)
            raise TransientHttpError(f"{verb} {host}: {reason}") from error
        if len(payload) > limit:
            raise HttpError(f"response from {host} exceeded {limit} bytes")
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


def _time_left(method: str, host: str | None) -> float | None:
    """Seconds left before the current deadline; refuses to start anything once it has passed."""
    limit = execution.current()
    if limit is None:
        return None
    left = limit.remaining()
    if left <= 0:
        raise HttpError(f"{method} {host}: not sent; the {limit.label} deadline of {limit.seconds:g}s passed")
    return left


def _check_late(method: str, host: str | None) -> None:
    limit = execution.current()
    if limit is not None and limit.expired():
        raise HttpError(
            f"{method} {host}: the {limit.label} deadline of {limit.seconds:g}s passed mid-response"
        )


def _read_body(stream: Any, limit: int, method: str, host: str | None) -> bytes:
    """Read at most ``limit + 1`` bytes; under a deadline, one receive at a time, checking it in between."""
    read1 = getattr(stream, "read1", None)
    if execution.current() is None or not callable(read1):
        payload: bytes = stream.read(limit + 1)
        return payload
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = read1(min(_READ_CHUNK, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        _check_late(method, host)
    return b"".join(chunks)


def check_retries(method: str, retries: int) -> None:
    """Refuse retry counts outside 0 to 3, and any retry of a method that is not GET or HEAD."""
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise ValueError(f"retries must be a whole number from 0 to {MAX_RETRIES}")
    if retries and method.upper() not in IDEMPOTENT_METHODS:
        raise ValueError(f"only GET and HEAD requests can be retried, not {method.upper()}")


def backoff_seconds(attempt: int) -> float:
    """Jittered exponential wait before retry number ``attempt`` (from 0): 0.25-0.5 s, 0.5-1 s, 1-2 s."""
    base = 0.5 * 2 ** min(attempt, 4)
    return _JITTER.uniform(base / 2, base)


def parse_retry_after(value: str | None, *, moment: datetime | None = None) -> float | None:
    """Seconds asked for by a ``Retry-After`` header (a delay or an HTTP date); None if absent or invalid."""
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit() and len(text) <= 12:
        return float(int(text))
    try:
        until = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return max(0.0, (until - (moment or now())).total_seconds())


def request_with_retries(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    retries: int,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
    allow_http: bool = False,
    follow_redirects: bool = True,
    public_only: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpResponse:
    """Send an idempotent GET or HEAD, repeating it up to ``retries`` times after a transient failure.

    Repeated: :class:`TransientHttpError` and the statuses in :data:`RETRY_STATUSES`. Each repeat waits a
    jittered backoff, or the server's ``Retry-After`` when that is at most :data:`MAX_RETRY_AFTER_SECONDS`.
    A wait that would not leave at least a second of the current deadline is not made: the last response
    is returned, or the last error raised. Works with any :class:`HttpTransport`, including test fakes.
    """
    verb = method.upper()
    check_retries(verb, retries)
    attempt = 0
    while True:
        try:
            response = transport.request(
                verb,
                url,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                allow_http=allow_http,
                follow_redirects=follow_redirects,
                public_only=public_only,
            )
        except TransientHttpError:
            delay = backoff_seconds(attempt)
            if attempt >= retries or not _can_wait(delay):
                raise
        else:
            if attempt >= retries or response.status not in RETRY_STATUSES:
                return response
            asked = parse_retry_after(response.headers.get("retry-after"))
            if asked is not None and asked > MAX_RETRY_AFTER_SECONDS:
                return response
            delay = backoff_seconds(attempt) if asked is None else asked
            if not _can_wait(delay):
                return response
        sleep(delay)
        attempt += 1


def _can_wait(delay: float) -> bool:
    left = execution.remaining()
    return left is None or delay + _MIN_ATTEMPT_SECONDS <= left
