"""SMTP: implicit TLS versus STARTTLS, app passwords versus XOAUTH2, and the envelope."""

from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime
from typing import Any

import pytest

from switchboard.connectors.mail import smtp as mail_smtp
from switchboard.connectors.mail.compose import compose
from switchboard.connectors.mail.imap import Credentials
from switchboard.connectors.mail.smtp import connect_smtp
from switchboard.errors import UncertainOutcomeError
from tests.fixtures.mail.fakes import FakeSmtp

NOW = datetime(2026, 11, 11, 13, 45, tzinfo=UTC)


class _Forbidden:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the other SMTP class must not be used")


def test_starttls_upgrades_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []

    class PlainSmtp(FakeSmtp):
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            super().__init__()
            self.address = (host, port, kwargs)
            created.append(self)

    monkeypatch.setattr(smtplib, "SMTP", PlainSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Forbidden)
    client = connect_smtp("smtp.example.com", 587, security="starttls", timeout=5)
    assert client is created[0]
    assert created[0].address == ("smtp.example.com", 587, {"timeout": 5})
    assert created[0].calls == ["ehlo", "starttls", "ehlo"]


def test_ssl_uses_implicit_tls_with_certificate_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []

    class TlsSmtp(FakeSmtp):
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            super().__init__()
            self.address = (host, port)
            self.options = kwargs
            created.append(self)

    monkeypatch.setattr(smtplib, "SMTP_SSL", TlsSmtp)
    monkeypatch.setattr(smtplib, "SMTP", _Forbidden)
    connect_smtp("smtp.example.com", 465, security="ssl", timeout=5)
    assert created[0].address == ("smtp.example.com", 465)
    assert created[0].options["timeout"] == 5
    context = created[0].options["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert created[0].calls == ["ehlo"]


def _send(monkeypatch: pytest.MonkeyPatch, credentials: Credentials) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr(mail_smtp, "connect_smtp", lambda host, port, *, security, timeout: fake)
    message = compose(
        sender="alex@example.com", to=["sam@example.org"], subject="Hello", body="Hi Sam", now=NOW
    )
    mail_smtp.send(
        "smtp.example.com",
        465,
        "ssl",
        credentials,
        message,
        sender="alex@example.com",
        recipients=["sam@example.org", "records@example.com"],
        timeout=5,
    )
    return fake


def test_app_password_uses_login_and_the_explicit_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _send(monkeypatch, Credentials("alex@example.com", password="app-password-123"))
    assert fake.calls == [("login", "alex@example.com"), "send_message", "quit", "close"]
    assert fake.password == "app-password-123"
    message, sender, envelope = fake.sent[0]
    assert sender == "alex@example.com"
    assert envelope == ["sam@example.org", "records@example.com"]
    assert message["Bcc"] is None


def test_oauth_token_uses_xoauth2(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _send(monkeypatch, Credentials("alex@example.com", access_token="token-123"))
    assert fake.calls[0] == ("auth", "XOAUTH2")
    assert fake.auth_responses == ["user=alex@example.com\x01auth=Bearer token-123\x01\x01"]
    assert fake.password is None


def test_connection_is_closed_when_sending_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing(FakeSmtp):
        def send_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise smtplib.SMTPRecipientsRefused({"sam@example.org": (550, b"no such user")})

    fake = Refusing()
    monkeypatch.setattr(mail_smtp, "connect_smtp", lambda host, port, *, security, timeout: fake)
    message = compose(sender="alex@example.com", to=["sam@example.org"], subject="Hello", body="Hi", now=NOW)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        mail_smtp.send(
            "smtp.example.com",
            465,
            "ssl",
            Credentials("alex@example.com", password="app-password-123"),
            message,
            sender="alex@example.com",
            recipients=["sam@example.org"],
            timeout=5,
        )
    assert fake.calls[-2:] == ["quit", "close"]


def _send_failing(monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> FakeSmtp:
    """Run ``send`` against a server whose submission raises ``failure``; returns the fake for inspection."""

    class Failing(FakeSmtp):
        def send_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("send_message")
            raise failure

    fake = Failing()
    monkeypatch.setattr(mail_smtp, "connect_smtp", lambda host, port, *, security, timeout: fake)
    message = compose(sender="alex@example.com", to=["sam@example.org"], subject="Hello", body="Hi", now=NOW)
    try:
        mail_smtp.send(
            "smtp.example.com",
            465,
            "ssl",
            Credentials("alex@example.com", password="app-password-123"),
            message,
            sender="alex@example.com",
            recipients=["sam@example.org"],
            timeout=5,
        )
    finally:
        assert fake.calls[-2:] == ["quit", "close"]
    return fake


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
    ],
)
def test_a_connection_lost_during_submission_is_uncertain(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    with pytest.raises(UncertainOutcomeError, match="may have accepted it") as caught:
        _send_failing(monkeypatch, failure)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPDataError(554, b"5.7.1 message rejected"),
        smtplib.SMTPSenderRefused(550, b"5.1.0 sender rejected", "alex@example.com"),
        smtplib.SMTPRecipientsRefused({"sam@example.org": (550, b"no such user")}),
        smtplib.SMTPNotSupportedError("SMTPUTF8 not supported by server"),
    ],
)
def test_a_server_refusal_stays_a_definite_failure(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    with pytest.raises(type(failure)) as caught:
        _send_failing(monkeypatch, failure)
    assert caught.value is failure
    assert not isinstance(caught.value, UncertainOutcomeError)


def test_a_failed_sign_in_happens_before_submission_and_is_not_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rejecting(FakeSmtp):
        def login(self, user: str, password: str) -> tuple[int, bytes]:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    fake = Rejecting()
    monkeypatch.setattr(mail_smtp, "connect_smtp", lambda host, port, *, security, timeout: fake)
    message = compose(sender="alex@example.com", to=["sam@example.org"], subject="Hello", body="Hi", now=NOW)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        mail_smtp.send(
            "smtp.example.com",
            465,
            "ssl",
            Credentials("alex@example.com", password="app-password-123"),
            message,
            sender="alex@example.com",
            recipients=["sam@example.org"],
            timeout=5,
        )
    assert "send_message" not in fake.calls
    assert fake.calls[-2:] == ["quit", "close"]


def test_credentials_repr_hides_secrets() -> None:
    text = repr(Credentials("alex@example.com", password="app-password-123", access_token="token-123"))
    assert "app-password-123" not in text
    assert "token-123" not in text
