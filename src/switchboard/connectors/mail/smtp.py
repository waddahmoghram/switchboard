"""SMTP submission with :mod:`smtplib`.

* ``ssl`` connects with implicit TLS (port 465); ``starttls`` connects in plaintext and upgrades with
  STARTTLS before authenticating (port 587). smtplib raises when the server does not offer STARTTLS,
  so credentials never travel unencrypted. Certificates are verified with the system trust store.
* App passwords use smtplib's ``login`` (PLAIN or LOGIN inside TLS); OAuth tokens use ``AUTH XOAUTH2``.
* Recipients are passed as the SMTP envelope, so Bcc addresses never appear in the message headers.
* A server reply that refuses the message is a definite failure. A connection that breaks or times out
  during submission is not: the server may already have accepted the message, so :func:`send` raises
  :class:`~switchboard.errors.UncertainOutcomeError` and nothing is resent.

Tests replace :func:`connect_smtp`, or :class:`smtplib.SMTP` and :class:`smtplib.SMTP_SSL`, with fakes.
"""

from __future__ import annotations

import contextlib
import smtplib
import ssl
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from typing import Any, Protocol

from switchboard.connectors.mail.imap import Credentials
from switchboard.connectors.mail.oauth import XOAuth2
from switchboard.errors import ToolError, UncertainOutcomeError

# Replies in which the server refused the message. smtplib's exceptions all subclass OSError, so these are
# matched before the broader OSError that also covers dropped connections and timeouts.
DEFINITE_REFUSALS: tuple[type[BaseException], ...] = (
    smtplib.SMTPResponseException,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPNotSupportedError,
)
UNCERTAIN_SUBMISSION = (
    "the connection failed while the message was being submitted, so the server may have accepted it; "
    "check the Sent folder or ask a recipient before sending again; nothing was resent"
)


class SmtpClient(Protocol):
    """The part of :class:`smtplib.SMTP` this connector uses."""

    def ehlo(self, name: str = ...) -> Any: ...
    def starttls(self, *, context: ssl.SSLContext | None = ...) -> Any: ...
    def login(self, user: str, password: str) -> Any: ...
    def auth(
        self, mechanism: str, authobject: Callable[..., str], *, initial_response_ok: bool = ...
    ) -> Any: ...
    def send_message(
        self, msg: EmailMessage, from_addr: str | None = ..., to_addrs: Sequence[str] | None = ...
    ) -> Any: ...
    def quit(self) -> Any: ...
    def close(self) -> None: ...


def connect_smtp(host: str, port: int, *, security: str, timeout: float) -> SmtpClient:
    """Open a verified TLS session that has already said EHLO. Tests monkeypatch this function."""
    context = ssl.create_default_context()
    client: smtplib.SMTP
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, context=context, timeout=timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if security != "ssl":
            client.starttls(context=context)
            client.ehlo()  # RFC 3207: forget what was learned before TLS and ask again
    except BaseException:
        client.close()
        raise
    return client


def authenticate(client: SmtpClient, credentials: Credentials) -> None:
    if credentials.access_token is not None:
        client.auth("XOAUTH2", XOAuth2(credentials.username, credentials.access_token).smtp)
        return
    if credentials.password is None:
        raise ToolError("no password or OAuth token is configured")
    client.login(credentials.username, credentials.password)


def send(
    host: str,
    port: int,
    security: str,
    credentials: Credentials,
    message: EmailMessage,
    *,
    sender: str,
    recipients: Sequence[str],
    timeout: float,
) -> list[str]:
    """Deliver ``message`` to ``recipients`` (the envelope); return the addresses the server refused.

    smtplib raises only when every recipient is refused; otherwise it delivers to the rest and reports the
    refusals in its return value, which a caller must not mistake for full success. Failures while
    connecting or signing in happen before submission and propagate unchanged.
    """
    client = connect_smtp(host, port, security=security, timeout=timeout)
    try:
        authenticate(client, credentials)
        try:
            refused = client.send_message(message, from_addr=sender, to_addrs=list(recipients)) or {}
        except DEFINITE_REFUSALS:
            raise
        except OSError as exc:
            raise UncertainOutcomeError(UNCERTAIN_SUBMISSION) from exc
        return sorted(str(address) for address in refused)
    finally:
        with contextlib.suppress(Exception):
            client.quit()
        client.close()
