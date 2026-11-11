"""Pure IMAP syntax helpers: quoting, modified UTF-7 mailbox names, SEARCH criteria, and parsers for
the response shapes :mod:`imaplib` returns. Nothing here performs I/O.

imaplib sends command arguments verbatim (it quotes only the LOGIN password), so every string this
connector puts on the wire goes through :func:`quote` or :func:`mailbox_argument` first.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from switchboard.errors import ToolError

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delimiter>NIL|"(?:\\.|[^"\\])*") (?P<name>.*)', re.DOTALL)
_LITERAL_MARK = re.compile(rb"\{\d+\}")
_QUOTED_PAIR = re.compile(rb"\\(.)", re.DOTALL)
_UID = re.compile(rb"\bUID (\d+)")
_FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_APPENDUID = re.compile(rb"\[APPENDUID (\d+) (\d+)\]")
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]+")


def reject_controls(value: str, what: str) -> None:
    """Refuse CR, LF, and NUL, which can never appear inside an IMAP string or a mail header."""
    if any(character in value for character in "\r\n\x00"):
        raise ToolError(f"{what} must not contain line breaks or NUL characters")


def quote(value: str) -> str:
    """Return an IMAP quoted string (RFC 3501 section 4.3).

    Backslash and double quote are escaped. CR, LF, and NUL are refused, which also prevents a search
    term or mailbox name from smuggling a second command onto the connection. Quoted strings are
    7-bit, so non-ASCII text must be sent as a literal instead.
    """
    reject_controls(value, "IMAP strings")
    if not value.isascii():
        raise ValueError("quote() needs ASCII; send other text as a literal")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def encode_mailbox(name: str) -> str:
    """Encode a mailbox name in IMAP's modified UTF-7 (RFC 3501 section 5.1.3).

    Printable ASCII stands for itself except ``&``, written ``&-``. Other characters are UTF-16BE,
    base64 encoded with ``,`` instead of ``/`` and no padding, between ``&`` and ``-``.
    """
    out: list[str] = []
    pending: list[str] = []
    for character in name:
        if " " <= character <= "~":
            out.append(_shift(pending))
            pending.clear()
            out.append("&-" if character == "&" else character)
        else:
            pending.append(character)
    out.append(_shift(pending))
    return "".join(out)


def decode_mailbox(name: str) -> str:
    """Decode modified UTF-7; malformed sequences are kept verbatim rather than guessed."""
    out: list[str] = []
    position = 0
    while position < len(name):
        start = name.find("&", position)
        end = name.find("-", start) if start != -1 else -1
        if start == -1 or end == -1:
            out.append(name[position:])
            break
        out.append(name[position:start])
        out.append(_unshift(name[start : end + 1]))
        position = end + 1
    return "".join(out)


def _shift(characters: list[str]) -> str:
    if not characters:
        return ""
    encoded = base64.b64encode("".join(characters).encode("utf-16-be")).decode("ascii")
    return "&" + encoded.rstrip("=").replace("/", ",") + "-"


def _unshift(token: str) -> str:
    body = token[1:-1]
    if not body:
        return "&"
    padded = body.replace(",", "/") + "=" * (-len(body) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode("utf-16-be")
    except ValueError:  # binascii.Error and UnicodeDecodeError are both ValueErrors
        return token


def mailbox_argument(name: str) -> str:
    """The wire form of a mailbox name for EXAMINE and APPEND: modified UTF-7, then quoted."""
    reject_controls(name, "mailbox names")
    return quote(encode_mailbox(name))


def imap_date(value: date) -> str:
    """RFC 3501 ``date`` such as ``2-Nov-2026``, independent of the process locale."""
    return f"{value.day}-{_MONTHS[value.month - 1]}-{value.year}"


@dataclass(frozen=True)
class SearchQuery:
    """Arguments for ``UID SEARCH``. ``literal`` holds the one non-ASCII term; it must be sent last."""

    criteria: tuple[str, ...]
    charset: str | None = None
    literal: bytes | None = None

    def arguments(self) -> tuple[str, ...]:
        prefix = ("CHARSET", self.charset) if self.charset else ()
        return (*prefix, *self.criteria)


def build_search(
    *,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: date | None = None,
    before: date | None = None,
    unseen: bool = False,
) -> SearchQuery:
    """Build ``UID SEARCH`` criteria; every given condition must match.

    ASCII terms become quoted strings. One term may contain other characters: it is sent last, as a
    UTF-8 literal with ``CHARSET UTF-8`` (RFC 3501 section 6.4.4), because quoted strings are 7-bit and
    imaplib attaches at most one literal to a command. A second non-ASCII term is refused rather than
    dropped or mangled. ``before`` is exclusive and both dates compare the server's internal date.
    """
    criteria: list[str] = []
    wide: list[tuple[str, str]] = []
    for key, value in (("FROM", sender), ("TO", recipient), ("SUBJECT", subject), ("TEXT", text)):
        if value is None:
            continue
        reject_controls(value, f"the {key.lower()} search term")
        if value.isascii():
            criteria += [key, quote(value)]
        else:
            wide.append((key, value))
    if since is not None:
        criteria += ["SINCE", imap_date(since)]
    if before is not None:
        criteria += ["BEFORE", imap_date(before)]
    if unseen:
        criteria.append("UNSEEN")
    if len(wide) > 1:
        raise ToolError("only one search term may contain non-ASCII characters")
    if wide:
        key, value = wide[0]
        return SearchQuery((*criteria, key), "UTF-8", value.encode("utf-8"))
    return SearchQuery(tuple(criteria) or ("ALL",))


def uid_set(uids: Iterable[int]) -> str:
    return ",".join(str(uid) for uid in sorted(set(uids)))


def parse_search(data: Sequence[Any]) -> list[int]:
    """UIDs from ``uid('SEARCH', …)`` data such as ``[b'4 9 12']``, ``[b'']``, or ``[None]``."""
    found: set[int] = set()
    for item in data:
        if isinstance(item, bytes):
            found.update(int(token) for token in item.split() if token.isdigit())
    return sorted(found)


def parse_capabilities(data: Sequence[Any]) -> frozenset[str]:
    words: set[str] = set()
    for item in data:
        if isinstance(item, bytes):
            words.update(word.upper() for word in item.decode("ascii", "replace").split())
    return frozenset(words)


def first_int(data: Sequence[Any]) -> int | None:
    """The first all-digit item, as in ``[b'3857529045']`` from ``response('UIDVALIDITY')``."""
    for item in data:
        if isinstance(item, bytes) and item.strip().isdigit():
            return int(item.strip())
    return None


def describe(data: Any) -> str:
    """Printable server response text for error messages; literals (message content) are skipped."""
    items = data if isinstance(data, list) else [data]
    text = " ".join(item.decode("utf-8", "replace") for item in items if isinstance(item, bytes))
    return _UNPRINTABLE.sub(" ", text).strip()[:200] or "no details"


@dataclass(frozen=True)
class MailboxInfo:
    """A LIST entry. ``name`` is decoded for people; :func:`encode_mailbox` restores the wire form."""

    name: str
    flags: tuple[str, ...]

    def has_flag(self, flag: str) -> bool:
        return flag.lower() in {item.lower() for item in self.flags}

    @property
    def selectable(self) -> bool:
        return not (self.has_flag("\\Noselect") or self.has_flag("\\NonExistent"))


def parse_list(data: Sequence[Any]) -> list[MailboxInfo]:
    """Parse LIST data: ``(flags) "delimiter" name`` lines, or ``(prefix, literal)`` tuples for odd names."""
    mailboxes: list[MailboxInfo] = []
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            parsed = _parse_list_line(bytes(item[0]), bytes(item[1]))
        elif isinstance(item, bytes):
            parsed = _parse_list_line(item, None)
        else:
            parsed = None
        if parsed is not None:
            mailboxes.append(parsed)
    return mailboxes


def _parse_list_line(line: bytes, literal: bytes | None) -> MailboxInfo | None:
    match = _LIST_LINE.fullmatch(line)
    if match is None:
        return None
    raw = match.group("name")
    if literal is not None and _LITERAL_MARK.fullmatch(raw):
        raw = literal
    elif raw[:1] == b'"' and raw[-1:] == b'"' and len(raw) >= 2:
        raw = _QUOTED_PAIR.sub(rb"\1", raw[1:-1])
    flags = tuple(flag.decode("ascii", "replace") for flag in match.group("flags").split())
    return MailboxInfo(decode_mailbox(raw.decode("utf-8", "replace")), flags)


@dataclass(frozen=True)
class FetchedItem:
    uid: int
    flags: tuple[str, ...]
    size: int | None
    data: bytes | None


def parse_fetch(data: Sequence[Any]) -> dict[int, FetchedItem]:
    """Group imaplib FETCH data by UID.

    imaplib returns a flat list: a ``(prefix, literal)`` tuple for each response carrying a literal,
    then the rest of that response line as ``bytes`` (``b')'``, or more items such as FLAGS), and plain
    ``bytes`` for responses without a literal. Responses without a UID (unsolicited flag updates) are
    ignored; several responses for one UID are merged.
    """
    records: list[tuple[bytes, bytes | None]] = []
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:
            records.append((bytes(item[0]), bytes(item[1])))
        elif isinstance(item, bytes) and records and item[:1] in (b")", b" "):
            meta, literal = records[-1]
            records[-1] = (meta + item, literal)
        elif isinstance(item, bytes) and item.strip():
            records.append((item, None))
    found: dict[int, FetchedItem] = {}
    for meta, literal in records:
        uid_match = _UID.search(meta)
        if uid_match is None:
            continue
        uid = int(uid_match.group(1))
        previous = found.get(uid)
        flags_match = _FLAGS.search(meta)
        size_match = _SIZE.search(meta)
        found[uid] = FetchedItem(
            uid=uid,
            flags=_flags(flags_match.group(1)) if flags_match else (previous.flags if previous else ()),
            size=int(size_match.group(1)) if size_match else (previous.size if previous else None),
            data=literal if literal is not None else (previous.data if previous else None),
        )
    return found


def _flags(raw: bytes) -> tuple[str, ...]:
    return tuple(flag.decode("ascii", "replace") for flag in raw.split())


def parse_appenduid(data: Sequence[Any]) -> tuple[int, int] | None:
    """``(uidvalidity, uid)`` from an ``[APPENDUID …]`` response code (UIDPLUS, RFC 4315)."""
    for item in data:
        if isinstance(item, bytes):
            match = _APPENDUID.search(item)
            if match:
                return int(match.group(1)), int(match.group(2))
    return None
