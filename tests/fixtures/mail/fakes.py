"""Fakes for the mail connector's tests. Nothing here opens a socket.

:class:`FakeImap` answers with the data shapes :class:`imaplib.IMAP4` returns (``(typ, data)`` tuples,
``(prefix, literal)`` pairs followed by a ``b')'`` trailer, ``[None]`` for "no responses"), so the
connector's response parsing runs exactly as it would against a real server.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from switchboard.connectors.mail.accounts import Account, parse_account

FIXTURES = Path(__file__).resolve().parent
_PARTIAL = re.compile(r"BODY\.PEEK\[\]<0\.(\d+)>")
_FIELDS = re.compile(r"HEADER\.FIELDS \(([^)]*)\)")


def load(name: str) -> bytes:
    """Read a fixture message from tests/fixtures/mail."""
    return (FIXTURES / name).read_bytes()


def make_account(name: str = "personal", **overrides: Any) -> Account:
    """A valid account built through the real parser; ``None`` overrides remove a key."""
    table: dict[str, Any] = {
        "provider": "fastmail",
        "address": "alex@example.com",
        "password": "env:SWITCHBOARD_TEST_MAIL_PASSWORD",
    }
    table.update(overrides)
    account, problems = parse_account(name, {key: value for key, value in table.items() if value is not None})
    assert account is not None, problems
    return account


def simple_message(number: int) -> bytes:
    return (
        f"From: Sender {number} <sender{number}@example.com>\r\n"
        "To: alex@example.com\r\n"
        f"Subject: Message {number}\r\n"
        f"Message-ID: <m{number}@example.com>\r\n"
        "Date: Tue, 10 Nov 2026 09:00:00 +0000\r\n"
        "\r\n"
        f"Body {number}\r\n"
    ).encode("ascii")


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: tuple[str, ...] = ()


@dataclass
class FakeMailbox:
    """One mailbox. ``name`` is the wire name (modified UTF-7)."""

    name: str
    uidvalidity: int = 1
    attributes: tuple[str, ...] = ("\\HasNoChildren",)
    report_uidnext: bool = True
    uidnext: int | None = None  # None: one more than the highest UID, as on a fresh server
    messages: dict[int, FakeMessage] = field(default_factory=dict)

    def add(self, raw: bytes, *, flags: tuple[str, ...] = ()) -> int:
        uid = max(self.messages, default=0) + 1
        self.messages[uid] = FakeMessage(uid, raw, flags)
        return uid

    def next_uid(self) -> int:
        return self.uidnext if self.uidnext is not None else max(self.messages, default=0) + 1


class FakeImap:
    """An authenticated-capable stand-in for :class:`imaplib.IMAP4_SSL`."""

    def __init__(
        self,
        mailboxes: list[FakeMailbox] | Mapping[str, FakeMailbox],
        *,
        capabilities: str = "IMAP4rev1 SPECIAL-USE UIDPLUS",
    ) -> None:
        if isinstance(mailboxes, Mapping):
            self.mailboxes = dict(mailboxes)
        else:
            self.mailboxes = {box.name: box for box in mailboxes}
        self.capability_line = capabilities.encode("ascii")
        self.literal: Any = None
        self.calls: list[tuple[Any, ...]] = []
        self.logins: list[tuple[str, str]] = []
        self.authentications: list[tuple[str, bytes]] = []
        self.searches: list[tuple[tuple[str, ...], bytes | None]] = []
        self.appended: list[dict[str, Any]] = []
        self.selected: FakeMailbox | None = None
        self.readonly: bool | None = None
        self.logged_out = False
        self._responses: dict[str, list[Any]] = {}

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("LOGIN", user))
        self.logins.append((user, password))
        return "OK", [b"LOGIN completed"]

    def authenticate(
        self, mechanism: str, authobject: Callable[[bytes], bytes | None]
    ) -> tuple[str, list[bytes]]:
        self.calls.append(("AUTHENTICATE", mechanism))
        self.authentications.append((mechanism, authobject(b"") or b""))
        return "OK", [b"Success"]

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [self.capability_line]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]:
        self.calls.append(("EXAMINE" if readonly else "SELECT", mailbox))
        box = self.mailboxes.get(_unquote(mailbox))
        if box is None:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.selected, self.readonly = box, readonly
        self._responses = {"UIDVALIDITY": [str(box.uidvalidity).encode()]}
        if box.report_uidnext:
            self._responses["UIDNEXT"] = [str(box.next_uid()).encode()]
        return "OK", [str(len(box.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[Any]]:
        return code, self._responses.pop(code, [None])

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        literal, self.literal = self.literal, None
        self.calls.append(("UID", command, *args))
        if self.selected is None:
            raise imaplib.IMAP4.error(f"UID {command} illegal in state AUTH")
        if command == "SEARCH":
            self.searches.append((args, literal))
            found = self._search(self.selected, list(args), literal)
            return "OK", [" ".join(str(uid) for uid in found).encode()]
        if command == "FETCH":
            return "OK", self._fetch(self.selected, args[0], args[1]) or [None]
        raise AssertionError(f"FakeImap does not implement UID {command}")

    def append(self, mailbox: str, flags: str, date_time: str, message: bytes) -> tuple[str, list[bytes]]:
        name = _unquote(mailbox)
        self.calls.append(("APPEND", mailbox, flags))
        box = self.mailboxes.get(name)
        if box is None:
            return "NO", [b"[TRYCREATE] Mailbox does not exist"]
        uid = box.add(message, flags=tuple(flags.strip("()").split()))
        self.appended.append(
            {"mailbox": name, "flags": flags, "date_time": date_time, "message": message, "uid": uid}
        )
        return "OK", [f"[APPENDUID {box.uidvalidity} {uid}] APPEND completed".encode()]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"LOGOUT received"]

    def _search(self, box: FakeMailbox, tokens: list[str], literal: bytes | None) -> list[int]:
        if literal is not None:
            tokens.append(literal.decode("utf-8"))  # the literal is the value of the last key
        uids = sorted(box.messages)
        position = 0
        while position < len(tokens):
            key = tokens[position].upper()
            if key == "CHARSET":
                position += 2
            elif key == "ALL":
                position += 1
            elif key == "UNSEEN":
                uids = [uid for uid in uids if "\\Seen" not in box.messages[uid].flags]
                position += 1
            elif key == "UID":
                wanted = _uid_range(tokens[position + 1], box)
                uids = [uid for uid in uids if uid in wanted]
                position += 2
            elif key in ("SINCE", "BEFORE"):
                position += 2  # dates are accepted but not evaluated by the fake
            elif key in ("FROM", "TO", "SUBJECT", "TEXT"):
                needle = _unquote(tokens[position + 1]).lower()
                uids = [uid for uid in uids if needle in _search_field(box.messages[uid].raw, key).lower()]
                position += 2
            else:
                raise AssertionError(f"FakeImap cannot evaluate search key {key}")
        return uids

    def _fetch(self, box: FakeMailbox, uid_spec: str, items: str) -> list[Any]:
        wanted = {int(part) for part in uid_spec.split(",")}
        data: list[Any] = []
        for sequence, uid in enumerate(sorted(box.messages), start=1):
            if uid not in wanted:
                continue
            message = box.messages[uid]
            meta = f"{sequence} (UID {uid} FLAGS ({' '.join(message.flags)}) RFC822.SIZE {len(message.raw)}"
            section = _section(message.raw, items)
            if section is None:
                data.append(f"{meta})".encode())
                continue
            label, literal = section
            data.append((f"{meta} {label} {{{len(literal)}}}".encode(), literal))
            data.append(b")")
        return data

    # Defined last so the method name does not shadow the builtin in the annotations above.
    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[Any]]:
        self.calls.append(("LIST", directory, pattern))
        lines = [
            f'({" ".join(box.attributes)}) "/" {_quote(box.name)}'.encode() for box in self.mailboxes.values()
        ]
        return "OK", lines


class FakeImapServer:
    """Hands out a new :class:`FakeImap` per connection, all sharing the same mailboxes."""

    def __init__(self, mailboxes: list[FakeMailbox]) -> None:
        self.mailboxes = {box.name: box for box in mailboxes}
        self.connections: list[FakeImap] = []
        self.addresses: list[tuple[str, int, float]] = []

    def connect(self, host: str, port: int, *, timeout: float) -> FakeImap:
        self.addresses.append((host, port, timeout))
        self.connections.append(FakeImap(self.mailboxes))
        return self.connections[-1]


class FakeSmtp:
    """Records what the connector does with an SMTP connection."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.sent: list[tuple[EmailMessage, str | None, list[str]]] = []
        self.password: str | None = None
        self.auth_responses: list[str] = []
        self.connections: list[tuple[str, int, str]] = []
        self.refuse: dict[str, tuple[int, bytes]] = {}

    def connect(self, host: str, port: int, *, security: str, timeout: float) -> FakeSmtp:
        """Stands in for ``smtp.connect_smtp``: records the address and returns this fake."""
        self.connections.append((host, port, security))
        return self

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self.calls.append("ehlo")
        return 250, b"smtp.example.com"

    def starttls(self, *, context: Any = None) -> tuple[int, bytes]:
        self.calls.append("starttls")
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", user))
        self.password = password
        return 235, b"accepted"

    def auth(
        self, mechanism: str, authobject: Callable[..., str], *, initial_response_ok: bool = True
    ) -> tuple[int, bytes]:
        self.calls.append(("auth", mechanism))
        self.auth_responses.append(authobject())
        return 235, b"accepted"

    def send_message(
        self, msg: EmailMessage, from_addr: str | None = None, to_addrs: list[str] | None = None
    ) -> dict[str, Any]:
        self.calls.append("send_message")
        self.sent.append((msg, from_addr, list(to_addrs or [])))
        return {address: reply for address, reply in self.refuse.items() if address in (to_addrs or [])}

    def quit(self) -> tuple[int, bytes]:
        self.calls.append("quit")
        return 221, b"bye"

    def close(self) -> None:
        self.calls.append("close")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def _uid_range(spec: str, box: FakeMailbox) -> set[int]:
    """``n:*`` includes the highest UID even when it is below ``n`` (RFC 3501 section 6.4.8)."""
    if spec.endswith(":*"):
        low = min(int(spec[:-2]), max(box.messages, default=0))
        return {uid for uid in box.messages if uid >= low}
    if ":" in spec:
        start, end = (int(part) for part in spec.split(":"))
        return set(range(start, end + 1))
    return {int(spec)}


def _search_field(raw: bytes, key: str) -> str:
    if key == "TEXT":
        return raw.decode("utf-8", "replace")
    message = email.message_from_bytes(raw, policy=email.policy.default)
    return str(message.get(key.capitalize(), ""))


def _section(raw: bytes, items: str) -> tuple[str, bytes] | None:
    fields = _FIELDS.search(items)
    if fields is not None:
        wanted = fields.group(1).upper().split()
        return f"BODY[HEADER.FIELDS ({fields.group(1)})]", _header_fields(raw, set(wanted))
    partial = _PARTIAL.search(items)
    if partial is not None:
        return "BODY[]<0>", raw[: int(partial.group(1))]
    return None


def _header_fields(raw: bytes, wanted: set[str]) -> bytes:
    """The requested header lines (with continuations) plus the blank line, as a server returns them."""
    head = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0]
    kept: list[bytes] = []
    keep = False
    for line in head.splitlines():
        if line[:1] in (b" ", b"\t"):
            if keep:
                kept.append(line)
            continue
        keep = line.split(b":", 1)[0].decode("ascii", "replace").upper() in wanted
        if keep:
            kept.append(line)
    return b"".join(line + b"\r\n" for line in kept) + b"\r\n"
