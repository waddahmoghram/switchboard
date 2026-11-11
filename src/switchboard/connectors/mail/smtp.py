"""SMTP submission with :mod:`smtplib`.

* ``ssl`` connects with implicit TLS (port 465); ``starttls`` connects in plaintext and upgrades with
  STARTTLS before authenticating (port 587). smtplib raises when the server does not offer STARTTLS,
  so credentials never travel unencrypted. Certificates are verified with the system trust store.
* App passwords use smtplib's ``login`` (PLAIN or LOGIN inside TLS); OAuth tokens use ``AUTH XOAUTH2``.
* Recipients are passed as the SMTP envelope, so Bcc addresses never appear in the message headers.

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
from switchboard.errors import ToolError


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
    refusals in its return value, which a caller must not mistake for full success.
    """
    client = connect_smtp(host, port, security=security, timeout=timeout)
    try:
        authenticate(client, credentials)
        refused = client.send_message(message, from_addr=sender, to_addrs=list(recipients)) or {}
        return sorted(str(address) for address in refused)
    finally:
        with contextlib.suppress(Exception):
            client.quit()
        client.close()
