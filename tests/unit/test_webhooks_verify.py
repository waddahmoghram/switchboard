"""Webhook verifiers as pure functions: valid, wrong secret, stale, replay inputs, missing headers."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from switchboard.connectors.webhooks.hooks import Hook
from switchboard.connectors.webhooks.verify import (
    Reply,
    Request,
    check_github,
    check_google_calendar,
    check_hmac_sha256,
    check_msgraph,
    check_token_header,
    parse_json,
    parse_signature,
    safe_headers,
    same,
    sign,
    verify,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "webhooks"
SECRET = "test-secret"
NOW = 1_800_000_000.0
T = int(NOW)
BODY = b'{"event":"build","status":"passed"}'


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_non_finite_numbers_are_not_valid_webhook_json(literal: str) -> None:
    assert parse_json(f'{{"value":{literal}}}'.encode()) == (False, None)


def request(
    headers: dict[str, str] | None = None, body: bytes = BODY, query: dict[str, list[str]] | None = None
) -> Request:
    return Request(
        headers={name.lower(): value for name, value in (headers or {}).items()}, query=query or {}, body=body
    )


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Computed here independently of the module under test."""
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def hmac_check(header: str | None, body: bytes = BODY, now: float = NOW) -> tuple[bool, str]:
    headers = {} if header is None else {"X-Switchboard-Signature": header}
    result = check_hmac_sha256(SECRET, request(headers, body), now=now, tolerance=300)
    return result.ok, result.reason


def test_sign_matches_an_independent_hmac() -> None:
    assert sign(SECRET, T, BODY) == signature(SECRET, T, BODY)


def test_hmac_valid_signature_is_accepted_with_a_replay_key() -> None:
    header = signature(SECRET, T, BODY)
    result = check_hmac_sha256(SECRET, request({"X-Switchboard-Signature": header}), now=NOW, tolerance=300)

    assert result.ok
    assert result.replay_key == f"{T}:{header.split('v1=')[1]}"
    assert result.replay_until == T + 301
    assert result.reply is None


@pytest.mark.parametrize(
    ("header", "body", "reason"),
    [
        (signature("wrong-secret", T, BODY), BODY, "signature mismatch"),
        (signature(SECRET, T, BODY), b'{"event":"build","status":"failed"}', "signature mismatch"),
        (signature(SECRET, T - 301, BODY), BODY, "timestamp outside the tolerance window"),
        (signature(SECRET, T + 301, BODY), BODY, "timestamp outside the tolerance window"),
        (None, BODY, "missing X-Switchboard-Signature"),
        ("v1=" + "0" * 64, BODY, "malformed X-Switchboard-Signature"),
        (f"t=abc,v1={'0' * 64}", BODY, "malformed X-Switchboard-Signature"),
        (f"t={T},t={T},v1={'0' * 64}", BODY, "malformed X-Switchboard-Signature"),
        (f"t={T},v1=xyz", BODY, "malformed X-Switchboard-Signature"),
    ],
)
def test_hmac_refusals(header: str | None, body: bytes, reason: str) -> None:
    assert hmac_check(header, body) == (False, reason)


def test_hmac_tolerance_boundary_and_uppercase_hex() -> None:
    assert hmac_check(signature(SECRET, T - 300, BODY)) == (True, "")
    prefix, _, digest = signature(SECRET, T, BODY).partition("v1=")
    assert hmac_check(f"{prefix}v1={digest.upper()}") == (True, "")


def test_parse_signature_ignores_unknown_fields() -> None:
    assert parse_signature(f"t={T}, v1={'a' * 64}, v0=legacy") == (T, "a" * 64)
    assert parse_signature("garbage") is None


def test_github_documented_test_vector() -> None:
    # Secret, payload, and signature published in GitHub's "Validating webhook deliveries" guide.
    digest = "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    result = check_github(
        "It's a Secret to Everybody", request({"X-Hub-Signature-256": f"sha256={digest}"}, b"Hello, World!")
    )

    assert result.ok
    assert result.dedupe == f"github:{digest}"


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"X-Hub-Signature-256": "sha256=" + "0" * 64}, "signature mismatch"),
        (
            {"X-Hub-Signature-256": hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()},
            "signature mismatch",
        ),
        ({"X-Hub-Signature": "sha1=" + "0" * 40}, "missing X-Hub-Signature-256"),
    ],
)
def test_github_refusals(headers: dict[str, str], reason: str) -> None:
    result = check_github(SECRET, request(headers))
    assert (result.ok, result.reason) == (False, reason)


def test_github_wrong_secret_is_refused() -> None:
    digest = hmac.new(b"wrong-secret", BODY, hashlib.sha256).hexdigest()
    assert not check_github(SECRET, request({"X-Hub-Signature-256": f"sha256={digest}"})).ok


def test_token_header() -> None:
    def check(headers: dict[str, str]) -> tuple[bool, str]:
        result = check_token_header(SECRET, request(headers), header="x-backup-token")
        return result.ok, result.reason

    assert check({"X-Backup-Token": SECRET}) == (True, "")
    assert check({"X-Backup-Token": "wrong-secret"}) == (False, "token mismatch")
    assert check({"X-Backup-Token": "t\u00ebst-secret"}) == (False, "token mismatch")
    assert check({"X-Other": SECRET}) == (False, "missing x-backup-token header")


def test_token_in_the_query_string_is_never_accepted() -> None:
    result = check_token_header(SECRET, request(query={"token": [SECRET]}), header="x-backup-token")
    assert not result.ok


def test_msgraph_validation_handshake_echoes_the_decoded_token() -> None:
    token = "Validation: Testing client application reachability"
    result = check_msgraph("test-client-state", request(body=b"", query={"validationToken": [token]}))

    assert result.ok
    assert result.reply == Reply(200, token)


def test_msgraph_notifications_need_the_client_state_on_every_item() -> None:
    body = (FIXTURES / "msgraph_notification.json").read_bytes()

    accepted = check_msgraph("test-client-state", request(body=body))
    assert accepted.ok
    assert accepted.reply is None
    assert accepted.drop_keys == frozenset({"clientState"})
    # The lifecycle item has no id, so identical later bodies must not be mistaken for retries.
    assert accepted.dedupe is None

    only_changes = json.loads(body)
    only_changes["value"] = only_changes["value"][:1]
    with_ids = check_msgraph("test-client-state", request(body=json.dumps(only_changes).encode()))
    assert with_ids.dedupe is not None and with_ids.dedupe.startswith("graph:")
    reordered = check_msgraph("test-client-state", request(body=json.dumps(only_changes, indent=2).encode()))
    assert reordered.dedupe == with_ids.dedupe

    document = json.loads(body)
    document["value"][1]["clientState"] = "other"
    one_wrong = json.dumps(document).encode()
    assert check_msgraph("test-client-state", request(body=one_wrong)).reason == "clientState mismatch"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'{"value":[{"changeType":"created"}]}', "clientState mismatch"),
        (b'{"value":[]}', "not a change notification collection"),
        (b"not json", "not a change notification collection"),
        (b"", "not a change notification collection"),
    ],
)
def test_msgraph_refusals(body: bytes, reason: str) -> None:
    result = check_msgraph("test-client-state", request(body=body))
    assert (result.ok, result.reason) == (False, reason)


def test_msgraph_refuses_an_oversized_handshake() -> None:
    result = check_msgraph("test-client-state", request(body=b"", query={"validationToken": ["x" * 5000]}))
    assert not result.ok


def google_headers(state: str, token: str = SECRET, number: str = "10") -> dict[str, str]:
    return {
        "X-Goog-Channel-ID": "sb-test-channel-1",
        "X-Goog-Channel-Token": token,
        "X-Goog-Resource-ID": "sb-test-resource",
        "X-Goog-Resource-State": state,
        "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        "X-Goog-Message-Number": number,
    }


def test_google_calendar_change_is_accepted_with_a_dedupe_key() -> None:
    result = check_google_calendar(SECRET, request(google_headers("exists"), body=b""))

    assert result.ok
    assert result.reply is None
    assert result.dedupe == "gcal:sb-test-channel-1:10"


def test_google_calendar_sync_is_answered_without_an_event() -> None:
    result = check_google_calendar(SECRET, request(google_headers("sync", number="1"), body=b""))

    assert result.ok
    assert result.reply == Reply(200, "")


def test_google_calendar_refusals() -> None:
    wrong = check_google_calendar(SECRET, request(google_headers("sync", token="wrong-secret"), body=b""))
    assert (wrong.ok, wrong.reason) == (False, "channel token mismatch")
    headers = google_headers("exists")
    del headers["X-Goog-Channel-Token"]
    missing = check_google_calendar(SECRET, request(headers, body=b""))
    assert (missing.ok, missing.reason) == (False, "missing X-Goog-Channel-Token")


def test_verify_dispatches_on_the_hook_scheme() -> None:
    hook = Hook(name="ci", verify="hmac-sha256", secret="env:UNUSED")
    header = signature(SECRET, T, BODY)
    assert verify(hook, SECRET, request({"X-Switchboard-Signature": header}), now=NOW).ok
    unknown = Hook(name="odd", verify="basic", secret="env:UNUSED")
    assert not verify(unknown, SECRET, request(), now=NOW).ok


def test_same_is_safe_with_any_input() -> None:
    assert same("abc", "abc")
    assert not same("abc", "abd")
    assert not same("abc", "\u00e4")
    assert not same("abc", "\udcff")


def test_safe_headers_drop_credentials_signatures_and_addresses() -> None:
    headers = {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/abc123",
        "x-github-event": "push",
        "x-github-delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        "authorization": "Bearer sb-test",
        "proxy-authorization": "Basic sb-test",
        "cookie": "session=sb-test",
        "x-hub-signature-256": "sha256=" + "0" * 64,
        "x-hub-signature": "sha1=" + "0" * 40,
        "x-switchboard-signature": f"t={T},v1={'0' * 64}",
        "x-goog-channel-token": SECRET,
        "x-goog-resource-state": "exists",
        "x-api-key": "sb-test",
        "x-forwarded-for": "198.51.100.7",
        "cf-connecting-ip": "198.51.100.7",
        "x-backup-key": SECRET,
        "host": "hooks.example.test",
        "content-length": "42",
        "x-long": "a" * 1000,
    }

    kept = safe_headers(headers, drop=["X-Backup-Key"])

    assert kept == {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/abc123",
        "x-github-event": "push",
        "x-github-delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        "x-goog-resource-state": "exists",
        "x-long": "a" * 299 + "\u2026",
    }
