"""``switchboard mail oauth-login`` for Gmail, with Google's redirect to a real listener on 127.0.0.1.

The "browser" is a thread that follows the redirect the way Google would after the owner signs in or
cancels; the token endpoint is the shared FakeHttp, so nothing leaves the machine.
"""

from __future__ import annotations

import functools
import json
import stat
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from switchboard import cli
from switchboard.app import build_app
from switchboard.config import load_config
from switchboard.connectors.mail import cli as mail_cli
from switchboard.connectors.mail import oauth
from switchboard.connectors.mail.oauth import TokenStore
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration

TOKEN_URL = "https://oauth2.googleapis.com/token"
SECRET_ENV = "SB_TEST_MAIL_CLI_GOOGLE_SECRET"
GMAIL = {
    "provider": "gmail",
    "address": "alex@example.com",
    "auth": "oauth2",
    "client_id": "client.apps.example",
    "client_secret": f"env:{SECRET_ENV}",
}


class Browser:
    """Follows the sign-in URL's redirect from a background thread, answering with ``reply``."""

    def __init__(self, **reply: str) -> None:
        self.reply = reply
        self.query: dict[str, str] = {}
        self.threads: list[threading.Thread] = []

    def __call__(self, url: str) -> bool:
        self.query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        redirect = f"{self.query['redirect_uri']}/?{urlencode({'state': self.query['state'], **self.reply})}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy loopback
        thread = threading.Thread(target=lambda: opener.open(redirect, timeout=10).read(), daemon=True)
        thread.start()
        self.threads.append(thread)
        return True

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=10)


@pytest.fixture
def login(
    tmp_path: Path, data_dir: Path, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Browser], int]:
    """``switchboard --config <file> mail oauth-login personal`` with ``browser`` as the web browser."""
    monkeypatch.setenv(SECRET_ENV, "google-client-secret")
    monkeypatch.setattr(cli, "_app", lambda args: build_app(load_config(args.config), http=fake_http))
    # A redirect that never arrives then fails within 30 seconds instead of the command's 5 minutes.
    monkeypatch.setattr(mail_cli, "google_login", functools.partial(oauth.google_login, timeout=30))
    config = tmp_path / "config.toml"
    lines = [
        "[switchboard]",
        f"data_dir = {json.dumps(str(data_dir))}",
        'connectors = ["mail"]',
        "[connectors.mail.accounts.personal]",
        *(f"{key} = {json.dumps(value)}" for key, value in GMAIL.items()),
    ]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def invoke(browser: Browser) -> int:
        monkeypatch.setattr(mail_cli.webbrowser, "open", browser)
        try:
            return cli.main(["--config", str(config), "mail", "oauth-login", "personal"])
        finally:
            browser.join()

    return invoke


def test_gmail_sign_in_sends_the_client_secret_and_saves_private_tokens(
    login: Callable[[Browser], int],
    fake_http: FakeHttp,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_http.add(
        "POST",
        TOKEN_URL,
        body=json.dumps({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}),
    )
    browser = Browser(code="code-123")

    assert login(browser) == 0

    sent = parse_qs((fake_http.requests[0].body or b"").decode("ascii"))
    assert sent["code"] == ["code-123"]
    assert sent["client_id"] == ["client.apps.example"]
    assert sent["client_secret"] == ["google-client-secret"]
    assert sent["redirect_uri"] == [browser.query["redirect_uri"]]
    assert browser.query["redirect_uri"].startswith("http://127.0.0.1:")
    assert browser.query["login_hint"] == "alex@example.com"
    store = TokenStore(data_dir)
    saved = store.load("personal")
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == ("access-1", "refresh-1")
    path = store.path("personal")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out, err = capsys.readouterr()
    assert out.startswith(
        "Sign in with your browser. If it did not open, visit:\nhttps://accounts.google.com/"
    )
    assert out.endswith(f"signed in as alex@example.com; tokens saved to {path} (mode 600)\n")
    assert err == ""
    for value in ("access-1", "refresh-1", "code-123", "google-client-secret"):
        assert value not in out


@pytest.mark.parametrize(
    ("reply", "token", "message"),
    [
        pytest.param({"error": "access_denied"}, None, "sign-in failed: access_denied", id="cancelled"),
        pytest.param(
            {"code": "code-123", "state": "forged"},
            None,
            "the sign-in response does not belong to this request (state mismatch)",
            id="state-mismatch",
        ),
        pytest.param(
            {"code": "code-123"},
            {"error": "invalid_grant", "error_description": "Bad Request"},
            "the sign-in server refused the request (invalid_grant: Bad Request)",
            id="exchange-refused",
        ),
    ],
)
def test_a_failed_gmail_sign_in_reports_the_reason_and_saves_nothing(
    login: Callable[[Browser], int],
    fake_http: FakeHttp,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
    reply: dict[str, str],
    token: dict[str, Any] | None,
    message: str,
) -> None:
    if token is not None:
        fake_http.add("POST", TOKEN_URL, status=400, body=json.dumps(token))

    assert login(Browser(**reply)) == 1

    assert capsys.readouterr().err == f"error: {message}\n"
    assert [request.url for request in fake_http.requests] == ([TOKEN_URL] if token else [])
    assert not TokenStore(data_dir).path("personal").exists()
