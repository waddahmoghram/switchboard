"""OAuth: PKCE, the device flow, token refresh, token files, and the XOAUTH2 format."""

from __future__ import annotations

import base64
import json
import stat
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from switchboard.connectors.mail.oauth import (
    DEVICE_GRANT,
    GOOGLE,
    DeviceAuthorization,
    OAuthError,
    TokenSet,
    TokenStore,
    XOAuth2,
    authorization_url,
    code_challenge,
    make_code_verifier,
    microsoft,
    parse_redirect,
    poll_device_token,
    refresh_tokens,
    request_device_code,
    tokens_from_response,
    xoauth2_string,
)
from tests.fakes import FakeHttp

DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
OUTLOOK_SCOPES = (
    "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access"
)
# The worked example in Google's XOAUTH2 documentation:
# https://developers.google.com/workspace/gmail/imap/xoauth2-protocol
GOOGLE_XOAUTH2_EXAMPLE = (
    "dXNlcj1zb21ldXNlckBleGFtcGxlLmNvbQFhdXRoPUJlYXJlciB5YTI5LnZGOWRmdDRxbVRjMk52"
    "YjNSbGNrQmhkSFJoZG1semRHRXVZMjl0Q2cBAQ=="
)


class FakeClock:
    def __init__(self, start: float) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def form(body: bytes | None) -> dict[str, list[str]]:
    assert body is not None
    return parse_qs(body.decode("ascii"))


def test_pkce_matches_rfc_7636_appendix_b() -> None:
    octets = bytes(
        [116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186,
         22, 212, 37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121]
    )  # fmt: skip
    verifier = make_code_verifier(octets)
    assert verifier == "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_device_flow_waits_through_pending_and_slow_down(fake_http: FakeHttp) -> None:
    endpoints = microsoft("consumers")
    fake_http.add(
        "POST",
        DEVICE_URL,
        body=json.dumps(
            {
                "device_code": "device-123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
                "message": "To sign in, open https://microsoft.com/devicelogin and enter ABCD-EFGH.",
            }
        ),
    )
    fake_http.add("POST", TOKEN_URL, status=400, body=json.dumps({"error": "authorization_pending"}))
    fake_http.add("POST", TOKEN_URL, status=400, body=json.dumps({"error": "slow_down"}))
    fake_http.add(
        "POST",
        TOKEN_URL,
        body=json.dumps({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}),
    )
    clock = FakeClock(1000.0)
    device = request_device_code(fake_http, endpoints, "client-123")
    assert device.user_code == "ABCD-EFGH"
    tokens = poll_device_token(fake_http, endpoints, "client-123", device, sleep=clock.sleep, clock=clock.now)
    assert clock.sleeps == [5, 5, 10]
    assert tokens == TokenSet("access-1", "refresh-1", 1020.0 + 3600, "")
    assert form(fake_http.requests[0].body) == {"client_id": ["client-123"], "scope": [OUTLOOK_SCOPES]}
    assert form(fake_http.requests[1].body) == {
        "grant_type": [DEVICE_GRANT],
        "client_id": ["client-123"],
        "device_code": ["device-123"],
    }
    assert fake_http.requests[1].headers["content-type"] == "application/x-www-form-urlencoded"


def test_device_flow_stops_when_the_code_expires(fake_http: FakeHttp) -> None:
    device = DeviceAuthorization(
        "device-123", "ABCD-EFGH", "https://microsoft.com/devicelogin", 12, 5, "sign in"
    )
    fake_http.add(
        "POST", TOKEN_URL, status=400, body=json.dumps({"error": "authorization_pending"}), repeat=True
    )
    clock = FakeClock(1000.0)
    with pytest.raises(OAuthError, match="expired"):
        poll_device_token(
            fake_http, microsoft("consumers"), "client-123", device, sleep=clock.sleep, clock=clock.now
        )
    assert len(fake_http.requests) == 2


def test_device_flow_stops_when_sign_in_is_declined(fake_http: FakeHttp) -> None:
    device = DeviceAuthorization(
        "device-123", "ABCD-EFGH", "https://microsoft.com/devicelogin", 900, 5, "sign in"
    )
    fake_http.add("POST", TOKEN_URL, status=400, body=json.dumps({"error": "authorization_declined"}))
    clock = FakeClock(0.0)
    with pytest.raises(OAuthError, match="declined"):
        poll_device_token(
            fake_http, microsoft("consumers"), "client-123", device, sleep=clock.sleep, clock=clock.now
        )


def test_google_refresh_keeps_the_refresh_token_and_sends_no_scope(fake_http: FakeHttp) -> None:
    fake_http.add("POST", GOOGLE.token_url, body=json.dumps({"access_token": "access-2", "expires_in": 3599}))
    old = TokenSet("access-1", "refresh-1", 1000.0, "https://mail.google.com/")
    fresh = refresh_tokens(fake_http, GOOGLE, "client.apps.example", "client-secret-value", old, now=5000.0)
    assert fresh == TokenSet("access-2", "refresh-1", 8599.0, "https://mail.google.com/")
    assert form(fake_http.requests[0].body) == {
        "client_id": ["client.apps.example"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-1"],
        "client_secret": ["client-secret-value"],
    }


def test_microsoft_refresh_sends_scopes_and_keeps_the_rotated_refresh_token(fake_http: FakeHttp) -> None:
    fake_http.add(
        "POST",
        TOKEN_URL,
        body=json.dumps({"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}),
    )
    fresh = refresh_tokens(
        fake_http,
        microsoft("consumers"),
        "client-123",
        None,
        TokenSet("access-1", "refresh-1", 0.0),
        now=100.0,
    )
    assert (fresh.access_token, fresh.refresh_token, fresh.expires_at) == ("access-2", "refresh-2", 3700.0)
    sent = form(fake_http.requests[0].body)
    assert sent["scope"] == [OUTLOOK_SCOPES]
    assert "client_secret" not in sent


def test_a_rejected_refresh_token_asks_for_a_new_sign_in(fake_http: FakeHttp) -> None:
    fake_http.add(
        "POST",
        GOOGLE.token_url,
        status=400,
        body=json.dumps(
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        ),
    )
    with pytest.raises(OAuthError, match="oauth-login"):
        refresh_tokens(fake_http, GOOGLE, "client.apps.example", None, TokenSet("a", "r", 0.0), now=1.0)


def test_token_response_without_an_access_token_is_an_error() -> None:
    with pytest.raises(OAuthError):
        tokens_from_response({"token_type": "Bearer"}, now=0.0)


def test_refresh_happens_two_minutes_early() -> None:
    tokens = TokenSet("a", "r", 1000.0)
    assert not tokens.needs_refresh(879.0)
    assert tokens.needs_refresh(880.0)


def test_authorization_url_carries_pkce_state_and_login_hint() -> None:
    url = authorization_url(
        GOOGLE,
        "client.apps.example",
        "http://127.0.0.1:8765",
        challenge="challenge-1",
        state="state-1",
        login_hint="alex@example.com",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert parse_qs(parts.query) == {
        "client_id": ["client.apps.example"],
        "redirect_uri": ["http://127.0.0.1:8765"],
        "response_type": ["code"],
        "scope": ["https://mail.google.com/"],
        "code_challenge": ["challenge-1"],
        "code_challenge_method": ["S256"],
        "state": ["state-1"],
        "login_hint": ["alex@example.com"],
    }


def test_parse_redirect_checks_state_and_errors() -> None:
    assert parse_redirect("/?code=code-1&state=state-1", "state-1") == "code-1"
    with pytest.raises(OAuthError, match="state"):
        parse_redirect("/?code=code-1&state=forged", "state-1")
    with pytest.raises(OAuthError, match="access_denied"):
        parse_redirect("/?error=access_denied&state=state-1", "state-1")
    with pytest.raises(OAuthError, match="no authorization code"):
        parse_redirect("/?state=state-1", "state-1")


def test_token_file_has_mode_600_in_a_private_directory(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "data")
    path = store.save("personal", TokenSet("access-1", "refresh-1", 1234.5, "scope-a"))
    assert path == tmp_path / "data" / "mail" / "oauth" / "personal.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not (path.parent / ".personal.json.tmp").exists()
    assert store.load("personal") == TokenSet("access-1", "refresh-1", 1234.5, "scope-a")
    assert store.load("someone-else") is None


def test_token_files_reached_through_symbolic_links_are_refused(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    path = store.save("personal", TokenSet("access-1", "refresh-1", 1.0))
    target = tmp_path / "elsewhere.json"
    target.write_text(path.read_text())
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(OAuthError, match="symbolic links"):
        store.load("personal")
    store.save("personal", TokenSet("access-2", "refresh-2", 2.0))
    assert not path.is_symlink()
    assert json.loads(target.read_text())["access_token"] == "access-1"


def test_token_files_readable_by_others_are_refused(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    store.save("personal", TokenSet("access-1", "refresh-1", 1.0)).chmod(0o644)
    with pytest.raises(OAuthError, match="mode 600"):
        store.load("personal")


def test_a_directory_in_place_of_a_token_file_is_refused_and_closed(
    tmp_path: Path, open_descriptors: Callable[[], int]
) -> None:
    store = TokenStore(tmp_path)
    store.path("personal").mkdir(parents=True)
    baseline = open_descriptors()
    with pytest.raises(OAuthError, match="must be a regular file"):
        store.load("personal")
    assert open_descriptors() == baseline


def test_xoauth2_string_format() -> None:
    assert (
        xoauth2_string("alex@example.com", "token-123")
        == "user=alex@example.com\x01auth=Bearer token-123\x01\x01"
    )


def test_xoauth2_matches_googles_documented_example() -> None:
    decoded = base64.b64decode(GOOGLE_XOAUTH2_EXAMPLE).decode("ascii")
    token = decoded.split("auth=Bearer ", 1)[1].rstrip("\x01")
    rebuilt = base64.b64encode(xoauth2_string("someuser@example.com", token).encode("ascii")).decode("ascii")
    assert rebuilt == GOOGLE_XOAUTH2_EXAMPLE


def test_xoauth2_answers_an_error_challenge_with_an_empty_response() -> None:
    expected = "user=alex@example.com\x01auth=Bearer token-123\x01\x01"
    smtp_responder = XOAuth2("alex@example.com", "token-123")
    assert smtp_responder.smtp() == expected
    assert smtp_responder.smtp(b'{"status":"401"}') == ""
    imap_responder = XOAuth2("alex@example.com", "token-123")
    assert imap_responder.imap(b"") == expected.encode("ascii")
    assert imap_responder.imap(b'{"status":"401"}') == b""
    assert "token-123" not in repr(smtp_responder)
