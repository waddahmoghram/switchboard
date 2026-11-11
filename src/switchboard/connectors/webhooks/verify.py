"""Verify inbound webhook deliveries.

These are pure functions: the caller passes the resolved secret and the time (``now``, Unix seconds),
so tests can pin both. Every secret comparison goes through :func:`same`, a constant-time comparison.
Secrets are only ever read from headers or the body, never from the query string.

Verification schemes (``verify = ...`` in the hook table):

``hmac-sha256``
    ``X-Switchboard-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">``. The
    timestamp must be within the hook's tolerance; the receiver also refuses a repeated (t, v1).
``github``
    ``X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body>``.
``token-header``
    A configured header whose value must equal the secret (for senders limited to a static header).
``msgraph``
    Microsoft Graph change notifications: answer the ``validationToken`` handshake, and require the
    configured ``clientState`` on every notification in ``value``.
``google-calendar``
    Google Calendar push channels: ``X-Goog-Channel-Token`` must equal the secret; ``sync`` messages
    are acknowledged without an event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.connectors.webhooks.hooks import Hook
from switchboard.util import sha256_hex, truncate

SIGNATURE_HEADER = "x-switchboard-signature"
GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"
GOOGLE_CHANNEL_HEADER = "x-goog-channel-token"
GOOGLE_STATE_HEADER = "x-goog-resource-state"
GOOGLE_ID_HEADER = "x-goog-channel-id"
GOOGLE_NUMBER_HEADER = "x-goog-message-number"
GRAPH_QUERY_PARAMETER = "validationToken"
MAX_GRAPH_HANDSHAKE_CHARS = 4096
MAX_HEADERS = 40
MAX_HEADER_VALUE = 300

_DIGITS = re.compile(r"[0-9]{1,12}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SENSITIVE_HEADER = re.compile(
    r"auth|cookie|token|secret|signature|password|passwd|api[-_]?key|session|csrf"
    r"|forwarded|real-ip|connecting-ip|client-ip",
    re.IGNORECASE,
)
_NOISE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "expect",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(frozen=True)
class Request:
    """One delivery as the receiver sees it: lowercased header names, decoded query, raw body."""

    headers: Mapping[str, str]
    query: Mapping[str, list[str]]
    body: bytes


@dataclass(frozen=True)
class Reply:
    """What to answer: a status code and a short plain-text body."""

    status: int
    text: str = ""


@dataclass(frozen=True)
class Check:
    """The outcome of verifying one delivery.

    ``reply`` answers a handshake directly, without an event. ``dedupe`` makes provider retries
    idempotent. ``replay_key`` must not have been seen before ``replay_until`` (Unix seconds).
    ``drop_keys`` are JSON keys removed from the event payload because they hold the secret.
    """

    ok: bool
    reason: str = ""
    reply: Reply | None = None
    dedupe: str | None = None
    replay_key: str | None = None
    replay_until: int = 0
    drop_keys: frozenset[str] = frozenset()

    @classmethod
    def fail(cls, reason: str) -> Check:
        return cls(ok=False, reason=reason)


def verify(hook: Hook, secret: str, request: Request, *, now: float) -> Check:
    """Run the hook's verification scheme."""
    if hook.verify == "hmac-sha256":
        return check_hmac_sha256(secret, request, now=now, tolerance=hook.tolerance)
    if hook.verify == "github":
        return check_github(secret, request)
    if hook.verify == "token-header":
        return check_token_header(secret, request, header=hook.header)
    if hook.verify == "msgraph":
        return check_msgraph(secret, request)
    if hook.verify == "google-calendar":
        return check_google_calendar(secret, request)
    return Check.fail(f"unknown verification scheme {hook.verify!r}")


def same(expected: str, given: str) -> bool:
    """Constant-time equality that accepts any characters in the untrusted value."""
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8", errors="replace"))


def hmac_sha256_hex(secret: str, timestamp: int, body: bytes) -> str:
    """The v1 value of X-Switchboard-Signature: hex HMAC-SHA256 over ``b"<timestamp>." + body``."""
    signed = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The complete X-Switchboard-Signature header value a sender attaches."""
    return f"t={timestamp},v1={hmac_sha256_hex(secret, timestamp, body)}"


def parse_signature(value: str) -> tuple[int, str] | None:
    """Split ``t=<digits>,v1=<64 hex>``; None when malformed or when a field repeats."""
    fields: dict[str, str] = {}
    for item in value.split(","):
        key, separator, field_value = item.strip().partition("=")
        if not separator or key in fields:
            return None
        fields[key] = field_value.strip()
    timestamp = fields.get("t", "")
    signature = fields.get("v1", "").lower()
    if not _DIGITS.fullmatch(timestamp) or not _HEX64.fullmatch(signature):
        return None
    return int(timestamp), signature


def check_hmac_sha256(secret: str, request: Request, *, now: float, tolerance: int) -> Check:
    header = request.headers.get(SIGNATURE_HEADER)
    if header is None:
        return Check.fail("missing X-Switchboard-Signature")
    parsed = parse_signature(header)
    if parsed is None:
        return Check.fail("malformed X-Switchboard-Signature")
    timestamp, signature = parsed
    if abs(now - timestamp) > tolerance:
        return Check.fail("timestamp outside the tolerance window")
    if not same(hmac_sha256_hex(secret, timestamp, request.body), signature):
        return Check.fail("signature mismatch")
    return Check(ok=True, replay_key=f"{timestamp}:{signature}", replay_until=timestamp + tolerance + 1)


def check_github(secret: str, request: Request) -> Check:
    header = request.headers.get(GITHUB_SIGNATURE_HEADER)
    if header is None:
        return Check.fail("missing X-Hub-Signature-256")
    digest = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    if not same(f"sha256={digest}", header.strip()):
        return Check.fail("signature mismatch")
    # GitHub signs only the body, so a captured delivery could be resent under a new X-GitHub-Delivery
    # id; keying on the signature turns a resend, or a redelivery you request, into a duplicate.
    return Check(ok=True, dedupe=f"github:{digest}")


def check_token_header(secret: str, request: Request, *, header: str) -> Check:
    value = request.headers.get(header) if header else None
    if value is None:
        return Check.fail(f"missing {header or 'token'} header")
    if not same(secret, value.strip()):
        return Check.fail("token mismatch")
    return Check(ok=True)


def check_msgraph(secret: str, request: Request) -> Check:
    handshake = request.query.get(GRAPH_QUERY_PARAMETER)
    if handshake:
        if len(handshake[0]) > MAX_GRAPH_HANDSHAKE_CHARS:
            return Check.fail("validation token too long")
        # Graph wants the decoded token back verbatim as text/plain, with no trailing newline.
        return Check(ok=True, reply=Reply(200, handshake[0]))
    is_json, document = parse_json(request.body)
    items = document.get("value") if is_json and isinstance(document, dict) else None
    if not isinstance(items, list) or not items:
        return Check.fail("not a change notification collection")
    for item in items:
        state = item.get("clientState") if isinstance(item, dict) else None
        if not isinstance(state, str) or not same(secret, state):
            return Check.fail("clientState mismatch")
    return Check(ok=True, dedupe=_graph_dedupe(items), drop_keys=frozenset({"clientState"}))


def _graph_dedupe(items: list[Any]) -> str | None:
    """Key retries by subscription and notification id; None when any item has no id.

    Lifecycle notifications (``reauthorizationRequired``, ``missed``) carry no id, and two separate ones can
    have identical bodies, so hashing the body would silently drop a later, genuine notification.
    """
    keys: list[str] = []
    for item in items:
        notification_id = item.get("id")
        if not isinstance(notification_id, str) or not notification_id:
            return None
        keys.append(f"{item.get('subscriptionId', '')}:{notification_id}")
    return "graph:" + sha256_hex("\n".join(sorted(keys)))


def check_google_calendar(secret: str, request: Request) -> Check:
    value = request.headers.get(GOOGLE_CHANNEL_HEADER)
    if value is None:
        return Check.fail("missing X-Goog-Channel-Token")
    if not same(secret, value.strip()):
        return Check.fail("channel token mismatch")
    if request.headers.get(GOOGLE_STATE_HEADER, "").strip() == "sync":
        return Check(ok=True, reply=Reply(200, ""))
    channel = request.headers.get(GOOGLE_ID_HEADER, "").strip()[:200]
    number = request.headers.get(GOOGLE_NUMBER_HEADER, "").strip()[:20]
    return Check(ok=True, dedupe=f"gcal:{channel}:{number}" if channel and number else None)


def parse_json(body: bytes) -> tuple[bool, Any]:
    """``(True, value)`` when the body is JSON, else ``(False, None)``."""
    if not body.strip():
        return False, None
    try:
        return True, json.loads(body)
    except (ValueError, RecursionError):
        return False, None


def without_keys(value: Any, keys: frozenset[str]) -> Any:
    """A copy of decoded JSON with ``keys`` removed from every object, at any depth."""
    if isinstance(value, dict):
        return {key: without_keys(item, keys) for key, item in value.items() if key not in keys}
    if isinstance(value, list):
        return [without_keys(item, keys) for item in value]
    return value


def safe_headers(headers: Mapping[str, str], drop: Iterable[str] = ()) -> dict[str, str]:
    """Headers worth keeping in an event: no credentials, signatures, cookies, or client addresses."""
    dropped = {name.lower() for name in drop}
    kept: dict[str, str] = {}
    for name, value in headers.items():
        key = name.lower()
        if key in dropped or key in _NOISE_HEADERS or _SENSITIVE_HEADER.search(key):
            continue
        kept[key] = truncate(value, MAX_HEADER_VALUE)
        if len(kept) >= MAX_HEADERS:
            break
    return kept
