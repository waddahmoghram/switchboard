"""Shared WebDAV plumbing for the CalDAV (calendar) and CardDAV (contacts) connectors. Not a connector.

* Requests go through ``ctx.http`` with HTTP Basic authentication (RFC 7617); every provider this
  project documents (iCloud, Fastmail, self-hosted servers) accepts app-specific passwords over Basic.
* ``207 Multi-Status`` replies (RFC 4918 §13) are parsed with :mod:`xml.etree.ElementTree` using the
  ``DAV:``, CalDAV (RFC 4791), and CardDAV (RFC 6352) namespaces. ElementTree never fetches external
  entities; documents that declare a DOCTYPE or entities are refused anyway, because no WebDAV reply
  needs a DTD and internal entity expansion is a denial-of-service vector.
* Discovery follows RFC 6764 §6: the configured URL may already be a collection, a principal, or a
  home set; otherwise ``/.well-known/caldav`` (or ``carddav``) yields ``DAV:current-user-principal``
  (RFC 5397), whose home-set property lists the collections.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from switchboard.errors import HttpError, ToolError
from switchboard.httpclient import HttpResponse, HttpTransport, basic_auth

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CARDDAV = "urn:ietf:params:xml:ns:carddav"

_PREFIXES = {DAV: "D", CALDAV: "C", CARDDAV: "CR"}
XMLNS = " ".join(f'xmlns:{prefix}="{uri}"' for uri, prefix in _PREFIXES.items())


def qname(namespace: str, local: str) -> str:
    """ElementTree's ``{namespace}local`` form of an XML name."""
    return f"{{{namespace}}}{local}"


RESOURCETYPE = qname(DAV, "resourcetype")
DISPLAYNAME = qname(DAV, "displayname")
CURRENT_USER_PRINCIPAL = qname(DAV, "current-user-principal")
GETETAG = qname(DAV, "getetag")
GETCONTENTTYPE = qname(DAV, "getcontenttype")
COLLECTION = qname(DAV, "collection")

MAX_REDIRECTS = 5
MAX_REPLY_BYTES = 20_000_000
_REDIRECTS = {301, 302, 303, 307, 308}
_XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}
_STATUS = re.compile(r"\s(\d{3})(?:\s|$)")


class DavError(ToolError):
    """A WebDAV request failed in a way the user can fix: credentials, URL, permissions, or throttling.

    ``retry_after`` holds the seconds a throttled server (HTTP 429 or 503) asked clients to wait.
    """

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_after_seconds(response: HttpResponse) -> float | None:
    """The delta-seconds form of ``Retry-After`` (RFC 9110 §10.2.3), capped at a day; None otherwise.

    The HTTP-date form is ignored, which simply leaves the normal refresh interval in charge.
    """
    value = response.headers.get("retry-after", "").strip()
    return float(min(int(value), 86400)) if value.isascii() and value.isdigit() else None


def throttled(label: str, response: HttpResponse) -> DavError | None:
    """A :class:`DavError` carrying ``retry_after`` for HTTP 429 and 503 replies, else None."""
    if response.status not in {429, 503}:
        return None
    wait = retry_after_seconds(response)
    hint = f"; the server asked to wait {int(wait)} seconds" if wait else ""
    return DavError(
        f"{label}: the server is busy (HTTP {response.status}){hint}",
        status=response.status,
        retry_after=wait,
    )


def describe_status(label: str, status: int) -> str:
    """A short, secret-free explanation of an unexpected HTTP status."""
    if status == 401:
        return (
            f"{label}: the server rejected the username or password (HTTP 401); "
            "iCloud and Fastmail need an app-specific password"
        )
    if status == 403:
        return f"{label}: this account may not access the resource (HTTP 403)"
    if status == 404:
        return f"{label}: nothing exists at the configured URL (HTTP 404); check the url setting"
    return f"{label}: the server answered HTTP {status}"


def parse_xml(body: bytes) -> ET.Element:
    """Parse a server's XML reply, refusing any DOCTYPE or ENTITY declaration.

    A first pass with expat stops at the start of a document type declaration, before any entity can be
    expanded, wherever comments or processing instructions place it. Text inside CDATA (for example an HTML
    description quoting ``<!DOCTYPE html>``) is content, not a declaration, and passes.
    """
    try:
        body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DavError("the server sent XML that is not UTF-8") from exc
    _refuse_declarations(body)
    try:
        return ET.fromstring(body)  # noqa: S314 - DTDs are refused above; the runtime is stdlib-only
    except ET.ParseError as exc:
        raise DavError("the server sent malformed XML") from exc


def _refuse_declarations(body: bytes) -> None:
    def refuse(*_args: object) -> None:
        raise DavError("refusing XML that declares a DOCTYPE or entities")

    scanner = xml.parsers.expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = refuse
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(body, True)
    except xml.parsers.expat.ExpatError as exc:
        raise DavError("the server sent malformed XML") from exc


@dataclass(frozen=True)
class Resource:
    """One ``DAV:response`` of a multistatus reply.

    ``props`` holds only properties reported with a 2xx ``propstat`` status. ``href`` is absolute,
    resolved against ``base``, the URL the request was finally sent to.
    """

    href: str
    status: int
    base: str
    props: Mapping[str, ET.Element] = field(default_factory=dict)

    def text(self, name: str) -> str:
        element = self.props.get(name)
        return "" if element is None else "".join(element.itertext()).strip()

    def link(self, name: str) -> str | None:
        """The absolute URL inside a property that wraps a ``DAV:href``, such as a home set."""
        element = self.props.get(name)
        if element is None:
            return None
        href = (element.findtext(qname(DAV, "href")) or "").strip()
        return urllib.parse.urljoin(self.base, href) if href else None

    def types(self) -> set[str]:
        element = self.props.get(RESOURCETYPE)
        return set() if element is None else {child.tag for child in element}


def _status_code(line: str | None) -> int | None:
    match = _STATUS.search(line or "")
    return int(match.group(1)) if match else None


def parse_multistatus(body: bytes, base: str) -> list[Resource]:
    """Parse a ``207 Multi-Status`` body (RFC 4918 §14.16)."""
    root = parse_xml(body)
    if root.tag != qname(DAV, "multistatus"):
        raise DavError("expected a WebDAV multistatus reply")
    resources: list[Resource] = []
    for response in root.findall(qname(DAV, "response")):
        href = (response.findtext(qname(DAV, "href")) or "").strip()
        if not href:
            continue
        props: dict[str, ET.Element] = {}
        for propstat in response.findall(qname(DAV, "propstat")):
            code = _status_code(propstat.findtext(qname(DAV, "status")))
            prop = propstat.find(qname(DAV, "prop"))
            if prop is not None and (code is None or 200 <= code < 300):
                props.update((child.tag, child) for child in prop)
        status = _status_code(response.findtext(qname(DAV, "status"))) or 200
        resources.append(Resource(urllib.parse.urljoin(base, href), status, base, props))
    return resources


def _prefixed(name: str) -> str:
    namespace, _, local = name[1:].partition("}")
    return f"{_PREFIXES[namespace]}:{local}"


def propfind_body(props: Sequence[str]) -> bytes:
    inner = "".join(f"<{_prefixed(name)}/>" for name in props)
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n<D:propfind {XMLNS}><D:prop>{inner}</D:prop></D:propfind>'
    ).encode()


def href_element(url: str) -> str:
    """A ``DAV:href`` element carrying the path of ``url``, for multiget reports."""
    return f"<D:href>{escape(urllib.parse.urlsplit(url).path)}</D:href>"


def _default_port(parts: urllib.parse.SplitResult) -> int | None:
    return parts.port or {"https": 443, "http": 80}.get(parts.scheme)


def trusted_host(configured: str, candidate: str) -> bool:
    """True for the configured host or a sibling under its parent domain.

    iCloud answers on ``caldav.icloud.com`` but lists collections on per-account hosts such as
    ``p42-caldav.icloud.com``. Hosts with fewer than three labels, and IP addresses, have no siblings.
    """
    if candidate == configured:
        return True
    labels = configured.split(".")
    if len(labels) < 3 or configured.replace(".", "").isdigit():
        return False
    return candidate.endswith("." + ".".join(labels[1:]))


@dataclass(frozen=True)
class DavClient:
    """Authenticated WebDAV requests for one configured account.

    ``label`` names the account in error messages, which never include URLs or credentials.
    Redirects are followed here because urllib refuses to redirect PROPFIND and REPORT; credentials
    are only sent to the configured origin or, over HTTPS, to a :func:`trusted_host`.
    """

    http: HttpTransport
    base_url: str
    username: str
    password: str = field(repr=False)
    label: str
    allow_http: bool = False

    def check_target(self, url: str) -> None:
        base = urllib.parse.urlsplit(self.base_url)
        target = urllib.parse.urlsplit(url)
        same_origin = (base.scheme, base.hostname, _default_port(base)) == (
            target.scheme,
            target.hostname,
            _default_port(target),
        )
        if same_origin:
            return
        if target.scheme == "https" and trusted_host(base.hostname or "", target.hostname or ""):
            return
        raise DavError(
            f"{self.label}: refusing to send credentials to {target.hostname!r}; "
            "set url to that server directly if you trust it"
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        depth: str | None = None,
    ) -> tuple[HttpResponse, str]:
        """Send one request, following redirects; returns the response and the final URL."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.check_target(current)
            merged = {"Authorization": basic_auth(self.username, self.password), **dict(headers or {})}
            if depth is not None:
                merged["Depth"] = depth
            try:
                response = self.http.request(
                    method,
                    current,
                    headers=merged,
                    body=body,
                    max_bytes=MAX_REPLY_BYTES,
                    allow_http=self.allow_http,
                )
            except HttpError as exc:
                raise DavError(f"{self.label}: {exc}") from exc
            location = response.headers.get("location")
            if response.status not in _REDIRECTS or not location:
                return response, current
            current = urllib.parse.urljoin(current, location)
        raise DavError(f"{self.label}: too many redirects")

    def expect(self, response: HttpResponse, allowed: Collection[int]) -> None:
        if response.status in allowed:
            return
        busy = throttled(self.label, response)
        if busy is not None:
            raise busy
        raise DavError(describe_status(self.label, response.status), status=response.status)

    def propfind(self, url: str, props: Sequence[str], *, depth: str = "0") -> list[Resource]:
        response, final = self.request(
            "PROPFIND", url, body=propfind_body(props), headers=_XML_HEADERS, depth=depth
        )
        self.expect(response, {207})
        return parse_multistatus(response.body, final)

    def report(self, url: str, body: bytes, *, depth: str = "1") -> list[Resource]:
        response, final = self.request("REPORT", url, body=body, headers=_XML_HEADERS, depth=depth)
        self.expect(response, {207})
        return parse_multistatus(response.body, final)

    def put(self, url: str, body: bytes, content_type: str, *, create_only: bool = True) -> HttpResponse:
        """PUT a resource; ``create_only`` sends ``If-None-Match: *`` so nothing is overwritten."""
        headers = {"Content-Type": content_type}
        if create_only:
            headers["If-None-Match"] = "*"
        response, _ = self.request("PUT", url, body=body, headers=headers)
        if response.status == 412:
            raise DavError(f"{self.label}: an item with this name already exists (HTTP 412)", status=412)
        self.expect(response, {200, 201, 204})
        return response

    def get(self, url: str) -> HttpResponse:
        response, _ = self.request("GET", url)
        self.expect(response, {200})
        return response


@dataclass(frozen=True)
class Flavor:
    """What differs between CalDAV and CardDAV discovery."""

    well_known: str
    home_set: str
    collection_type: str
    noun: str
    extra_props: tuple[str, ...] = ()


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _first(resources: Sequence[Resource]) -> Resource | None:
    return resources[0] if resources else None


def discover(client: DavClient, flavor: Flavor) -> list[Resource]:
    """Find the collections behind the configured URL (RFC 6764 §6, RFC 4791 §6, RFC 6352 §7).

    Returns the matching collections sorted by URL, each with ``DAV:displayname`` and any
    ``flavor.extra_props`` the server reported.
    """
    props = [RESOURCETYPE, DISPLAYNAME, CURRENT_USER_PRINCIPAL, flavor.home_set, *flavor.extra_props]
    try:
        here = _first(client.propfind(client.base_url, props))
    except DavError as exc:
        if exc.status not in {404, 405, 501}:
            raise
        here = None
    if here is not None and flavor.collection_type in here.types():
        return [here]
    home = here.link(flavor.home_set) if here is not None else None
    if home is None:
        principal = here.link(CURRENT_USER_PRINCIPAL) if here is not None else None
        home = _home_set(client, principal or _principal_from_well_known(client, flavor), flavor)
    return _list_home(client, home, flavor)


def _principal_from_well_known(client: DavClient, flavor: Flavor) -> str:
    """RFC 6764 §6 step 5: PROPFIND the well-known context path, then the root if that is missing."""
    origin = _origin(client.base_url)
    for path in (flavor.well_known, "/"):
        try:
            found = _first(client.propfind(origin + path, [CURRENT_USER_PRINCIPAL]))
        except DavError as exc:
            if exc.status == 404:
                continue
            raise
        principal = found.link(CURRENT_USER_PRINCIPAL) if found is not None else None
        if principal:
            return principal
    raise DavError(
        f"{client.label}: the server did not reveal the account's principal; "
        f"set url to the {flavor.noun} collection itself"
    )


def _home_set(client: DavClient, principal: str, flavor: Flavor) -> str:
    found = _first(client.propfind(principal, [flavor.home_set]))
    home = found.link(flavor.home_set) if found is not None else None
    if home is None:
        raise DavError(f"{client.label}: the account does not list a {flavor.noun} home")
    return home


def _list_home(client: DavClient, home: str, flavor: Flavor) -> list[Resource]:
    found = client.propfind(home, [RESOURCETYPE, DISPLAYNAME, *flavor.extra_props], depth="1")
    collections = [item for item in found if flavor.collection_type in item.types()]
    if not collections:
        raise DavError(f"{client.label}: no {flavor.noun} collections were found")
    return sorted(collections, key=lambda item: item.href)


def collection_url(url: str) -> str:
    """``url`` with exactly one trailing slash, so relative member names resolve inside it."""
    return url.rstrip("/") + "/"
