"""Fetch pages whose address an assistant or a web page chose, vetting every hop.

A watch URL comes from the assistant, and a redirect target comes from whichever site answered, so neither
is trusted. Redirects are followed here, one hop at a time, instead of by the HTTP client: each
``Location`` must again be an ``https://`` URL with a public host name inside ``allowed_domains``. The
transport is asked for ``public_only`` requests, so names that resolve to loopback, private, or link-local
addresses (``127.0.0.1.nip.io``) are refused as well.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from switchboard.connectors.shopping.settings import Settings, is_domain
from switchboard.errors import HttpError, ToolError
from switchboard.httpclient import HttpResponse, HttpTransport

MAX_REDIRECTS = 5  # RFC 9309 asks robots.txt fetchers to follow at least five
LOCAL_SUFFIXES = ("localhost", "local", "localdomain", "lan", "home.arpa", "internal", "intranet", "corp")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def vet_watch_url(raw: str, settings: Settings) -> str:
    """Normalize a watch URL or raise ToolError with a short reason."""
    try:
        parts = urlsplit(raw.strip())
        host = (parts.hostname or "").rstrip(".")
        has_credentials = parts.username is not None or parts.password is not None
    except ValueError as exc:
        raise ToolError("the URL could not be parsed") from exc
    if parts.scheme.lower() != "https" or not host:
        raise ToolError("only https:// product URLs can be watched")
    if has_credentials:
        raise ToolError("URLs with embedded credentials are refused")
    if not is_domain(host) or any(host == suffix or host.endswith("." + suffix) for suffix in LOCAL_SUFFIXES):
        raise ToolError("the URL needs a public host name, not an IP address or a local network name")
    if not settings.allows_host(host):
        raise ToolError(f"{host} is not in allowed_domains")
    return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", parts.query, ""))


def get_vetted(
    http: HttpTransport,
    url: str,
    settings: Settings,
    *,
    headers: Mapping[str, str],
    max_bytes: int,
) -> HttpResponse:
    """GET ``url``, following at most five redirects, each vetted like a watch URL.

    Raises HttpError when a redirect leaves the vetted space or there are too many; the message names
    only the host that redirected, because product URLs can carry session or affiliate tokens.
    """
    for _ in range(MAX_REDIRECTS + 1):
        response = http.request(
            "GET", url, headers=headers, max_bytes=max_bytes, follow_redirects=False, public_only=True
        )
        location = response.headers.get("location")
        if response.status not in _REDIRECTS or not location:
            return response
        try:
            url = vet_watch_url(urljoin(url, location), settings)
        except ToolError as exc:
            raise HttpError(f"refused a redirect from {urlsplit(url).hostname}: {exc}") from exc
    raise HttpError(f"{urlsplit(url).hostname} redirected more than {MAX_REDIRECTS} times")
