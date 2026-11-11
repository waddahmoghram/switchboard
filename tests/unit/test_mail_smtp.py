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


def test_credentials_repr_hides_secrets() -> None:
    text = repr(Credentials("alex@example.com", password="app-password-123", access_token="token-123"))
    assert "app-password-123" not in text
    assert "token-123" not in text
