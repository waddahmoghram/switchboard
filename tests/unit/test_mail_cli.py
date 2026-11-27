"""``switchboard mail oauth-login`` and ``switchboard mail check`` as the owner runs them.

The sign-in server, the browser, the clock, and the IMAP server are fakes, so nothing leaves the machine
and the device flow's waits take no time. The sign-in commands read no keyboard input: the device flow
prints a code to type into a browser, and Google's flow waits for the browser's redirect, which
``tests/integration/test_mail_cli_google.py`` covers against a real listener on 127.0.0.1.
"""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from switchboard import cli
from switchboard.app import App, build_app
from switchboard.config import load_config
from switchboard.connectors import mail
from switchboard.connectors.mail import cli as mail_cli
from switchboard.connectors.mail import imap as mail_imap
from switchboard.connectors.mail.oauth import TokenSet, TokenStore
from switchboard.errors import ConfigError
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.mail.fakes import FakeImap, FakeImapServer, FakeMailbox

DEVICE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
PASSWORD_ENV = "SB_TEST_MAIL_CLI_PASSWORD"
CLIENT_SECRET_ENV = "SB_TEST_MAIL_CLI_CLIENT_SECRET"
OUTLOOK = {"provider": "outlook", "address": "alex@example.com", "auth": "oauth2", "client_id": "client-123"}
GMAIL = {
    "provider": "gmail",
    "address": "alex@example.com",
    "auth": "oauth2",
    "client_id": "client.apps.example",
    "client_secret": f"env:{CLIENT_SECRET_ENV}",
}
FASTMAIL = {"provider": "fastmail", "address": "alex@example.com", "password": f"env:{PASSWORD_ENV}"}
DEVICE_MESSAGE = "To sign in, open https://microsoft.com/devicelogin and enter the code ABCD-EFGH."
DEVICE_CODE = {
    "device_code": "device-code-secret",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 5,
    "message": DEVICE_MESSAGE,
}
TOKENS = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}
SIGN_IN_URL = "https://accounts.google.com/o/oauth2/v2/auth?state=example"

Main = Callable[..., int]


class FakeTime:
    """Replaces the ``time`` module in the command: ``sleep`` advances ``time()`` instead of waiting."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGoogle:
    """Stands in for ``google_login``: records the call, announces, and asks to open the browser once."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.opened: list[object] = []

    def __call__(self, _http: object, client_id: str, client_secret: str | None, **options: Any) -> TokenSet:
        self.calls.append(
            {"client_id": client_id, "client_secret": client_secret, "login_hint": options["login_hint"]}
        )
        options["announce"](f"Sign in with your browser. If it did not open, visit:\n{SIGN_IN_URL}")
        self.opened.append(options["open_browser"](SIGN_IN_URL))
        return TokenSet("access-1", "refresh-1", 2_000_000_000.0, "https://mail.google.com/")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    # Starting at the real time keeps saved tokens fresh for a later ``check``, which uses the real clock.
    fake = FakeTime(float(int(time.time())))
    monkeypatch.setattr(mail_cli, "time", fake)
    return fake


@pytest.fixture
def main(tmp_path: Path, data_dir: Path, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> Main:
    """``switchboard --config <file> mail <argv>`` with ``account`` as ``personal`` and no real network."""
    monkeypatch.setattr(cli, "_app", lambda args: build_app(load_config(args.config), http=fake_http))

    def invoke(account: dict[str, Any], *argv: str) -> int:
        path = tmp_path / "config.toml"
        lines = [
            "[switchboard]",
            f"data_dir = {json.dumps(str(data_dir))}",
            'connectors = ["mail"]',
            "[connectors.mail.accounts.personal]",
            *(f"{key} = {json.dumps(value)}" for key, value in account.items()),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cli.main(["--config", str(path), "mail", *argv])

    return invoke


def mail_config(account: dict[str, Any]) -> dict[str, Any]:
    accounts = {"personal": account}
    return {"switchboard": {"connectors": ["mail"]}, "connectors": {"mail": {"accounts": accounts}}}


def run(app: App, *argv: str) -> int:
    """Parse ``switchboard mail <argv>`` and run its handler against ``app``."""
    args = cli.build_parser([mail]).parse_args(["mail", *argv])
    return int(args.app_handler(args, app))


def imap_server(monkeypatch: pytest.MonkeyPatch, *mailboxes: FakeMailbox) -> FakeImapServer:
    server = FakeImapServer(list(mailboxes))
    monkeypatch.setattr(mail_imap, "connect_imap", server.connect)
    return server


def test_device_sign_in_shows_the_code_then_saves_private_tokens(
    make_app: AppFactory,
    fake_http: FakeHttp,
    clock: FakeTime,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_http.add("POST", DEVICE_URL, body=json.dumps(DEVICE_CODE))
    fake_http.add("POST", TOKEN_URL, status=400, body=json.dumps({"error": "authorization_pending"}))
    fake_http.add("POST", TOKEN_URL, body=json.dumps(TOKENS))

    assert run(make_app(mail_config(OUTLOOK)), "oauth-login", "personal") == 0

    store = TokenStore(data_dir)
    path = store.path("personal")
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        DEVICE_MESSAGE,
        f"signed in as alex@example.com; tokens saved to {path} (mode 600)",
    ]
    assert err == ""
    assert parse_qs((fake_http.requests[0].body or b"").decode("ascii"))["client_id"] == ["client-123"]
    assert clock.sleeps == [5, 5]
    assert store.load("personal") == TokenSet("access-1", "refresh-1", clock.start + 10 + 3600, "")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for value in ("access-1", "refresh-1", "device-code-secret"):
        assert value not in out + err


def test_a_sign_in_without_a_refresh_token_succeeds_with_a_warning(
    make_app: AppFactory,
    fake_http: FakeHttp,
    clock: FakeTime,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_http.add("POST", DEVICE_URL, body=json.dumps(DEVICE_CODE))
    fake_http.add("POST", TOKEN_URL, body=json.dumps({"access_token": "access-1", "expires_in": 3600}))

    assert run(make_app(mail_config(OUTLOOK)), "oauth-login", "personal") == 0

    saved = TokenStore(data_dir).load("personal")
    assert saved is not None
    assert (saved.access_token, saved.refresh_token) == ("access-1", None)
    assert capsys.readouterr().err == (
        "warning: no refresh token was issued, so you must sign in again within the hour\n"
    )


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize(
    ("device", "token", "message"),
    [
        pytest.param(
            (200, DEVICE_CODE),
            (400, {"error": "authorization_declined"}),
            "sign-in was declined in the browser",
            id="declined",
        ),
        pytest.param(
            (200, DEVICE_CODE),
            (400, {"error": "expired_token"}),
            "the sign-in code expired; run oauth-login again",
            id="expired",
        ),
        pytest.param(
            (200, DEVICE_CODE),
            (400, {"error": "invalid_grant", "error_description": "The code was already used."}),
            "the sign-in server refused the request (invalid_grant: The code was already used.)",
            id="token-refused",
        ),
        pytest.param(
            (400, {"error": "invalid_client", "error_description": "AADSTS700016: no such app.\r\nTrace: 1"}),
            None,
            "the sign-in server refused the request (invalid_client: AADSTS700016: no such app.)",
            id="unknown-client",
        ),
        pytest.param(
            (200, {"user_code": "ABCD-EFGH"}),
            None,
            "the device authorization response was incomplete",
            id="incomplete",
        ),
        pytest.param(
            (502, "<html>Bad gateway</html>"),
            None,
            "login.microsoftonline.com answered HTTP 502 without a JSON body",
            id="not-json",
        ),
    ],
)
def test_a_failed_device_sign_in_reports_the_reason_and_saves_nothing(
    main: Main,
    fake_http: FakeHttp,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
    device: tuple[int, Any],
    token: tuple[int, Any] | None,
    message: str,
) -> None:
    for url, reply in ((DEVICE_URL, device), (TOKEN_URL, token)):
        if reply is not None:
            status, body = reply
            text = body if isinstance(body, str) else json.dumps(body)
            fake_http.add("POST", url, status=status, body=text)

    assert main(OUTLOOK, "oauth-login", "personal") == 1

    out, err = capsys.readouterr()
    assert err == f"error: {message}\n"
    assert "signed in" not in out
    assert not TokenStore(data_dir).path("personal").exists()


def test_interrupting_the_wait_for_sign_in_exits_130_and_saves_nothing(
    main: Main, fake_http: FakeHttp, clock: FakeTime, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(clock, "sleep", interrupted)
    fake_http.add("POST", DEVICE_URL, body=json.dumps(DEVICE_CODE))

    assert main(OUTLOOK, "oauth-login", "personal") == 130

    assert [request.url for request in fake_http.requests] == [DEVICE_URL]
    assert not TokenStore(data_dir).path("personal").exists()


@pytest.mark.parametrize(
    ("flags", "opened"),
    [pytest.param([], [SIGN_IN_URL], id="browser"), pytest.param(["--no-browser"], [], id="no-browser")],
)
def test_google_sign_in_sends_the_resolved_client_secret_and_honours_no_browser(
    make_app: AppFactory,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    opened: list[str],
) -> None:
    monkeypatch.setenv(CLIENT_SECRET_ENV, "google-client-secret")
    google = FakeGoogle()
    browser: list[str] = []

    def open_browser(url: str) -> bool:
        browser.append(url)
        return True

    monkeypatch.setattr(mail_cli, "google_login", google)
    monkeypatch.setattr(mail_cli.webbrowser, "open", open_browser)

    assert run(make_app(mail_config(GMAIL)), "oauth-login", "personal", *flags) == 0

    assert google.calls == [
        {
            "client_id": "client.apps.example",
            "client_secret": "google-client-secret",
            "login_hint": "alex@example.com",
        }
    ]
    assert browser == opened
    assert google.opened == [bool(opened)]
    path = TokenStore(data_dir).path("personal")
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "Sign in with your browser. If it did not open, visit:",
        SIGN_IN_URL,
        f"signed in as alex@example.com; tokens saved to {path} (mode 600)",
    ]
    assert "google-client-secret" not in out
    saved = TokenStore(data_dir).load("personal")
    assert saved is not None
    assert saved.scope == "https://mail.google.com/"


def test_an_unresolvable_client_secret_stops_before_the_browser_opens(
    main: Main, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(CLIENT_SECRET_ENV, raising=False)
    google = FakeGoogle()
    monkeypatch.setattr(mail_cli, "google_login", google)

    assert main(GMAIL, "oauth-login", "personal") == 1

    assert capsys.readouterr().err == f"error: environment variable '{CLIENT_SECRET_ENV}' is not set\n"
    assert google.calls == []
    assert not TokenStore(data_dir).path("personal").exists()


def test_a_password_account_is_told_to_switch_to_oauth_first(
    make_app: AppFactory, fake_http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(make_app(mail_config(FASTMAIL)), "oauth-login", "personal") == 1

    assert capsys.readouterr().err == 'account personal uses auth = "password"; set auth = "oauth2" first\n'
    assert fake_http.requests == []


@pytest.mark.parametrize("command", ["oauth-login", "check"])
def test_the_commands_refuse_to_run_without_the_mail_connector(make_app: AppFactory, command: str) -> None:
    with pytest.raises(ConfigError, match="the mail connector is not enabled"):
        run(make_app({}), command, "personal")


@pytest.mark.parametrize("command", ["oauth-login", "check"])
@pytest.mark.parametrize(
    ("account", "name", "message"),
    [
        pytest.param(FASTMAIL, "work", "unknown mail account 'work'; configured: personal", id="unknown"),
        pytest.param(
            {**OUTLOOK, "auth": "password", "password": f"env:{PASSWORD_ENV}"},
            "personal",
            "mail account 'personal' is misconfigured; run 'switchboard doctor'",
            id="misconfigured",
        ),
    ],
)
def test_an_unusable_account_name_is_an_error_not_a_crash(
    main: Main,
    fake_http: FakeHttp,
    capsys: pytest.CaptureFixture[str],
    command: str,
    account: dict[str, Any],
    name: str,
    message: str,
) -> None:
    assert main(account, command, name) == 1

    assert capsys.readouterr().err == f"error: {message}\n"
    assert fake_http.requests == []


@pytest.mark.parametrize(
    ("mailboxes", "settings", "drafts"),
    [
        pytest.param([("INBOX", ()), ("Brouillons", ("\\Drafts",))], {}, "Brouillons", id="special-use"),
        pytest.param([("INBOX", ()), ("Archive", ())], {}, "not found; set drafts_mailbox", id="none"),
        pytest.param([("INBOX", ())], {"drafts_mailbox": "Saved"}, "Saved", id="configured"),
    ],
)
def test_check_reports_the_mailbox_count_and_where_drafts_go(
    make_app: AppFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mailboxes: list[tuple[str, tuple[str, ...]]],
    settings: dict[str, str],
    drafts: str,
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "app-password")
    server = imap_server(
        monkeypatch,
        *(FakeMailbox(name, attributes=("\\HasNoChildren", *flags)) for name, flags in mailboxes),
    )

    assert run(make_app(mail_config({**FASTMAIL, **settings})), "check", "personal") == 0

    assert capsys.readouterr().out == (
        f"personal: signed in to imap.fastmail.com:993; {len(mailboxes)} mailboxes\n"
        f"drafts mailbox: {drafts}\n"
    )
    connection = server.connections[0]
    assert connection.logins == [('"alex@example.com"', "app-password")]
    assert connection.logged_out


def test_check_before_signing_in_says_how_to_sign_in(
    main: Main, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = imap_server(monkeypatch, FakeMailbox("INBOX"))

    assert main(OUTLOOK, "check", "personal") == 1

    assert capsys.readouterr().err == (
        "error: personal: OAuthError: not signed in yet; run 'switchboard mail oauth-login personal'\n"
    )
    assert server.addresses == []


def test_check_reports_a_connection_failure_with_the_account_name(
    main: Main, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "app-password")

    def refuse(_host: str, _port: int, *, timeout: float) -> FakeImap:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(mail_imap, "connect_imap", refuse)

    assert main(FASTMAIL, "check", "personal") == 1

    err = capsys.readouterr().err
    assert err == "error: personal: ConnectionRefusedError: connection refused\n"
    assert "app-password" not in err


def test_check_signs_in_with_the_tokens_that_oauth_login_saved(
    main: Main,
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: FakeTime,
) -> None:
    fake_http.add("POST", DEVICE_URL, body=json.dumps(DEVICE_CODE))
    fake_http.add("POST", TOKEN_URL, body=json.dumps(TOKENS))
    server = imap_server(monkeypatch, FakeMailbox("INBOX"), FakeMailbox("Drafts"))

    assert main(OUTLOOK, "oauth-login", "personal") == 0
    assert main(OUTLOOK, "check", "personal") == 0

    assert clock.sleeps == [5]
    assert [address[:2] for address in server.addresses] == [("outlook.office365.com", 993)]
    assert server.connections[0].authentications == [
        ("XOAUTH2", b"user=alex@example.com\x01auth=Bearer access-1\x01\x01")
    ]
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "personal: signed in to outlook.office365.com:993; 2 mailboxes",
        "drafts mailbox: Drafts",
    ]
    assert len(fake_http.requests) == 2
