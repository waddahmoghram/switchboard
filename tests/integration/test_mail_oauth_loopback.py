"""Google's installed-app flow end to end against a real loopback listener on 127.0.0.1.

The "browser" is a thread that follows the redirect the way Google would after sign-in; the token
endpoint is the FakeHttp from the shared fixtures, so nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from switchboard.connectors.mail.oauth import OAuthError, code_challenge, google_login
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration

TOKEN_URL = "https://oauth2.googleapis.com/token"


class Browser:
    """Opens the redirect URI with the given code and state from a background thread."""

    def __init__(self, *, state: str | None = None) -> None:
        self.forced_state = state
        self.query: dict[str, str] = {}
        self.threads: list[threading.Thread] = []

    def __call__(self, url: str) -> bool:
        self.query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        state = self.forced_state or self.query["state"]
        redirect = f"{self.query['redirect_uri']}/?code=code-123&state={state}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )  # never route loopback via a proxy
        thread = threading.Thread(target=lambda: opener.open(redirect, timeout=10).read(), daemon=True)
        thread.start()
        self.threads.append(thread)
        return True

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=10)


def test_loopback_flow_exchanges_the_code_with_the_pkce_verifier(fake_http: FakeHttp) -> None:
    fake_http.add(
        "POST",
        TOKEN_URL,
        body=json.dumps({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}),
    )
    browser = Browser()
    tokens = google_login(
        fake_http,
        "client.apps.example",
        None,
        login_hint="alex@example.com",
        open_browser=browser,
        announce=lambda _text: None,
        timeout=30,
    )
    browser.join()
    assert (tokens.access_token, tokens.refresh_token) == ("access-1", "refresh-1")
    assert browser.query["redirect_uri"].startswith("http://127.0.0.1:")
    assert browser.query["code_challenge_method"] == "S256"
    assert browser.query["login_hint"] == "alex@example.com"
    sent = parse_qs((fake_http.requests[0].body or b"").decode("ascii"))
    assert sent["code"] == ["code-123"]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["redirect_uri"] == [browser.query["redirect_uri"]]
    assert code_challenge(sent["code_verifier"][0]) == browser.query["code_challenge"]
    assert "client_secret" not in sent


def test_loopback_flow_rejects_a_forged_state(fake_http: FakeHttp) -> None:
    browser = Browser(state="forged")
    with pytest.raises(OAuthError, match="state"):
        google_login(
            fake_http,
            "client.apps.example",
            None,
            login_hint=None,
            open_browser=browser,
            announce=lambda _text: None,
            timeout=30,
        )
    browser.join()
    assert fake_http.requests == []
