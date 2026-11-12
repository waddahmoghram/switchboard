"""IMAP access on top of :mod:`imaplib`, read-only wherever the protocol allows.

* Connections use implicit TLS (port 993, RFC 8314) with certificate verification against the system
  trust store. Plaintext IMAP and STARTTLS on port 143 are not offered.
* Mailboxes are opened with ``EXAMINE`` and messages fetched with ``BODY.PEEK``, so reading never sets
  ``\\Seen`` or changes anything else. The only write is ``APPEND`` to the Drafts mailbox. A connection
  that fails during ``APPEND`` leaves its outcome unknown, so it raises
  :class:`~switchboard.errors.UncertainOutcomeError`; a ``NO`` or ``BAD`` reply is a definite failure.
* App passwords use ``LOGIN``; OAuth tokens use ``AUTHENTICATE XOAUTH2``.

Tests replace :func:`connect_imap` with a fake that returns imaplib-shaped data.
"""

from __future__ import annotations

import contextlib
import imaplib
import ssl
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from switchboard.connectors.mail import protocol
from switchboard.connectors.mail.oauth import XOAuth2
from switchboard.connectors.mail.protocol import FetchedItem, MailboxInfo, SearchQuery
from switchboard.errors import ToolError, UncertainOutcomeError

SUMMARY_FIELDS = "DATE FROM TO CC SUBJECT MESSAGE-ID"
THREAD_FIELDS = "MESSAGE-ID REFERENCES"
FETCH_LIMIT_BYTES = 10 * 1024 * 1024
UNCERTAIN_APPEND = (
    "the connection failed during APPEND, so the server may have stored the draft; "
    "check the Drafts mailbox before trying again; nothing was resent"
)


class ImapConnection(Protocol):
    """The part of :class:`imaplib.IMAP4` this connector uses (``Sequence`` avoids the ``list`` method)."""

    literal: Any

    def login(self, user: str, password: str) -> Any: ...
    def authenticate(self, mechanism: str, authobject: Callable[[bytes], bytes | None]) -> Any: ...
    def capability(self) -> tuple[str, Sequence[Any]]: ...
    def select(self, mailbox: str = ..., readonly: bool = ...) -> tuple[str, Sequence[Any]]: ...
    def response(self, code: str) -> tuple[str, Sequence[Any]]: ...
    def uid(self, command: str, *args: str) -> tuple[str, Sequence[Any]]: ...
    def append(self, mailbox: str, flags: str, date_time: str, message: bytes) -> Any: ...
    def logout(self) -> Any: ...
    def list(self, directory: str = ..., pattern: str = ...) -> Any: ...


def connect_imap(host: str, port: int, *, timeout: float) -> ImapConnection:
    """Open a verified implicit-TLS connection. Tests monkeypatch this function."""
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)


@dataclass(frozen=True)
class Credentials:
    """A login for one protocol. ``repr`` hides the password and the token."""

    username: str
    password: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)


def login(conn: ImapConnection, credentials: Credentials) -> None:
    if credentials.access_token is not None:
        conn.authenticate("XOAUTH2", XOAuth2(credentials.username, credentials.access_token).imap)
        return
    if credentials.password is None:
        raise ToolError("no password or OAuth token is configured")
    if not (credentials.username.isascii() and credentials.password.isascii()):
        raise ToolError("IMAP LOGIN needs an ASCII username and password")
    # imaplib quotes the password itself but sends the user name verbatim.
    conn.login(protocol.quote(credentials.username), credentials.password)


@dataclass(frozen=True)
class MailboxState:
    uidvalidity: int
    uidnext: int | None
    exists: int


class ImapSession:
    """Operations on one authenticated connection. Raises :class:`ToolError` on NO responses."""

    def __init__(self, conn: ImapConnection) -> None:
        self.conn = conn

    def capabilities(self) -> frozenset[str]:
        return protocol.parse_capabilities(_ok(self.conn.capability(), "CAPABILITY"))

    def list_mailboxes(self) -> list[MailboxInfo]:
        """LIST everything, asking for RFC 6154 special-use attributes when the server has SPECIAL-USE.

        Servers may include them in a plain LIST, but only ``RETURN (SPECIAL-USE)`` guarantees them.
        """
        pattern = "* RETURN (SPECIAL-USE)" if "SPECIAL-USE" in self.capabilities() else "*"
        return protocol.parse_list(_ok(self.conn.list('""', pattern), "LIST"))

    def examine(self, mailbox: str) -> MailboxState:
        """Open ``mailbox`` read-only (EXAMINE) and report UIDVALIDITY, UIDNEXT, and the message count."""
        typ, data = self.conn.select(protocol.mailbox_argument(mailbox), readonly=True)
        if typ != "OK":
            raise ToolError(f"cannot open mailbox {mailbox!r}: {protocol.describe(data)}")
        uidvalidity = protocol.first_int(self.conn.response("UIDVALIDITY")[1])
        if uidvalidity is None:
            raise ToolError(f"the server did not report UIDVALIDITY for {mailbox!r}")
        return MailboxState(
            uidvalidity=uidvalidity,
            uidnext=protocol.first_int(self.conn.response("UIDNEXT")[1]),
            exists=protocol.first_int(data) or 0,
        )

    def search(self, query: SearchQuery) -> list[int]:
        if query.literal is not None:
            self.conn.literal = query.literal  # imaplib appends it as ``{n}`` after the last argument
        return protocol.parse_search(_ok(self.conn.uid("SEARCH", *query.arguments()), "SEARCH"))

    def uids_after(self, last_uid: int) -> list[int]:
        """UIDs above ``last_uid``, ascending.

        ``UID n:*`` always includes the newest message even when its UID is below ``n`` (RFC 3501
        section 6.4.8), so the result is filtered.
        """
        found = self.search(SearchQuery(("UID", f"{last_uid + 1}:*")))
        return [uid for uid in found if uid > last_uid]

    def highest_uid(self) -> int:
        return max(self.search(SearchQuery(("ALL",))), default=0)

    def fetch_headers(self, uids: Sequence[int], fields: str = SUMMARY_FIELDS) -> dict[int, FetchedItem]:
        if not uids:
            return {}
        items = f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({fields})])"
        return protocol.parse_fetch(_ok(self.conn.uid("FETCH", protocol.uid_set(uids), items), "FETCH"))

    def fetch_message(self, uid: int, max_bytes: int = FETCH_LIMIT_BYTES) -> FetchedItem | None:
        """The first ``max_bytes`` of one message, or None when the UID does not exist.

        A partial fetch (``BODY.PEEK[]<0.n>``) bounds memory for huge messages; compare ``size`` with
        ``len(data)`` to see whether the message was cut.
        """
        items = f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{max_bytes}>)"
        found = protocol.parse_fetch(_ok(self.conn.uid("FETCH", str(uid), items), "FETCH"))
        return found.get(uid)

    def append(self, mailbox: str, message: bytes, *, flags: str, when: datetime) -> int | None:
        """APPEND a message; returns its UID when the server reports one (UIDPLUS, RFC 4315)."""
        try:
            result = self.conn.append(
                protocol.mailbox_argument(mailbox), flags, imaplib.Time2Internaldate(when), message
            )
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise UncertainOutcomeError(UNCERTAIN_APPEND) from exc
        appended = protocol.parse_appenduid(_ok(result, "APPEND"))
        return appended[1] if appended else None


def _ok(result: Any, command: str) -> Sequence[Any]:
    typ, data = result
    if typ != "OK":
        raise ToolError(f"IMAP {command} failed: {protocol.describe(data)}")
    return data if isinstance(data, list) else [data]


@contextlib.contextmanager
def open_session(host: str, port: int, credentials: Credentials, *, timeout: float) -> Iterator[ImapSession]:
    """Connect, sign in, and always log out, even when the body raises."""
    conn = connect_imap(host, port, timeout=timeout)
    try:
        login(conn, credentials)
        yield ImapSession(conn)
    finally:
        with contextlib.suppress(Exception):
            conn.logout()
