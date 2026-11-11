"""Turn raw RFC 5322 messages into small, safe summaries.

* :data:`email.policy.default` decodes RFC 2047 encoded words in headers and RFC 2231 parameters.
* The body is the ``text/plain`` part when there is one; otherwise ``text/html`` converted to text with
  :mod:`html.parser` (script and style dropped, block elements become line breaks, whitespace
  collapsed). Nothing is rendered, fetched, or executed.
* Attachments are reported as metadata (file name, content type, decoded size), never content.
* Control characters other than tab and newline are removed so terminal escapes in a message cannot
  reach anyone's screen.

Malformed input never raises: a header that cannot be parsed becomes an empty value.
"""

from __future__ import annotations

import email
import email.policy
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from email.header import decode_header, make_header
from email.message import MIMEPart
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from switchboard.util import iso

MAX_ADDRESSES = 50
MAX_ATTACHMENTS = 50
MAX_HEADER_CHARS = 998
MAX_DEPTH = 20
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACES = re.compile(r"[ \t\f\v\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
        "p", "pre", "section", "table", "tbody", "thead", "tfoot", "tr", "ul",
    }
)  # fmt: skip
_SKIPPED_TAGS = frozenset({"script", "style", "template", "noscript"})
_CELL_TAGS = frozenset({"td", "th"})


@dataclass(frozen=True)
class Attachment:
    filename: str | None
    content_type: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "content_type": self.content_type, "size": self.size}


@dataclass(frozen=True)
class Headers:
    message_id: str
    subject: str
    sender: str
    to: list[str]
    cc: list[str]
    date: str | None
    references: str


@dataclass(frozen=True)
class ParsedMessage:
    headers: Headers
    text: str
    truncated: bool
    attachments: list[Attachment]


def parse_headers(raw: bytes) -> Headers:
    """Parse a header block (or a whole message) into decoded header values."""
    return _headers(email.message_from_bytes(raw, policy=email.policy.default))


def parse_message(raw: bytes, *, max_chars: int) -> ParsedMessage:
    """Parse a whole message: decoded headers, body text capped at ``max_chars``, attachment metadata."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = _body_part(message)
    text = _body_text(body) if body is not None else ""
    attachments = [_attachment(part) for part in _leaf_parts(message, 0) if _is_attachment(part, body)]
    return ParsedMessage(
        headers=_headers(message),
        text=text[:max_chars],
        truncated=len(text) > max_chars,
        attachments=attachments[:MAX_ATTACHMENTS],
    )


def clean(text: str) -> str:
    """Drop control characters except tab and newline, and normalize line endings."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def html_to_text(markup: str) -> str:
    """Readable text from HTML: no scripts or styles, line breaks for block elements, tidy spacing."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = (_SPACES.sub(" ", line).strip() for line in clean("".join(parser.pieces)).split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


class _TextExtractor(HTMLParser):
    """Collects text pieces. ``<head>`` ends at ``</head>`` or at ``<body>``, as in browsers, so a
    sloppy message that never closes its head still shows its body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self._skipping = 0
        self._in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("head", "body"):
            self._in_head = tag == "head"
        elif tag in _SKIPPED_TAGS:
            self._skipping += 1
        elif tag == "br" or tag in _BLOCK_TAGS:
            self.pieces.append("\n")
        elif tag in _CELL_TAGS:
            self.pieces.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
        elif tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping and not self._in_head:
            self.pieces.append(data)


def _headers(message: MIMEPart) -> Headers:
    return Headers(
        message_id=_header_text(message, "Message-ID"),
        subject=_header_text(message, "Subject"),
        sender=next(iter(_addresses(message, "From")), _header_text(message, "From")),
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        date=_date(message),
        references=_header_text(message, "References"),
    )


def _header_text(message: MIMEPart, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:  # the header parser can fail on hostile input; treat as absent
        return ""
    if value is None:
        return ""
    text = " ".join(clean(_decode_words(str(value))).split())
    return text[:MAX_HEADER_CHARS]


def _decode_words(value: str) -> str:
    """Decode RFC 2047 words that a lenient header class left encoded."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # malformed encoded words: keep the raw text
        return value


def _addresses(message: MIMEPart, name: str) -> list[str]:
    try:
        values = [str(value) for value in message.get_all(name, [])]
    except Exception:  # unparseable address header
        return []
    formatted: list[str] = []
    for display, address in getaddresses(values):
        if not address and not display:
            continue
        display = " ".join(clean(display).split())
        address = clean(address).strip()
        formatted.append(f"{display} <{address}>" if display else address)
    return formatted[:MAX_ADDRESSES]


def _date(message: MIMEPart) -> str | None:
    raw = _header_text(message, "Date")
    if not raw:
        return None
    try:
        moment = parsedate_to_datetime(raw)
        if moment.tzinfo is None:  # "-0000" means the zone is unknown; read it as UTC
            moment = moment.replace(tzinfo=UTC)
        return iso(moment)
    except (TypeError, ValueError, IndexError, OverflowError):  # e.g. year 9999 shifted past UTC's range
        return None


def _body_part(message: MIMEPart) -> MIMEPart | None:
    try:
        return message.get_body(preferencelist=("plain",)) or message.get_body(preferencelist=("html",))
    except Exception:  # broken MIME structure: no body rather than a crash
        return None


def _body_text(part: MIMEPart) -> str:
    try:
        content = part.get_content()
    except Exception:  # unknown or malformed charset (even an RFC 2231 tuple), bad transfer encoding
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() == "html":
        return html_to_text(content)
    return clean(content).strip()


def _leaf_parts(part: MIMEPart, depth: int) -> Iterator[MIMEPart]:
    """Non-multipart parts in order; an attached message (message/rfc822) counts as one part."""
    if depth > MAX_DEPTH:
        return
    if part.get_content_maintype() == "multipart":
        for child in part.iter_parts():
            yield from _leaf_parts(child, depth + 1)
    else:
        yield part


def _is_attachment(part: MIMEPart, body: MIMEPart | None) -> bool:
    """Explicit attachments, plus named parts without a disposition; inline images are not attachments."""
    if part is body:
        return False
    disposition = part.get_content_disposition()
    return disposition == "attachment" or (disposition is None and _filename(part) is not None)


def _attachment(part: MIMEPart) -> Attachment:
    return Attachment(filename=_filename(part), content_type=part.get_content_type(), size=_size(part))


def _filename(part: MIMEPart) -> str | None:
    try:
        name = part.get_filename()
    except Exception:  # malformed parameters
        return None
    if not name:
        return None
    return " ".join(clean(_decode_words(name)).split())[:255] or None


def _size(part: MIMEPart) -> int:
    try:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return len(payload)
        return len(part.as_bytes())
    except Exception:  # truncated or malformed content; size unknown
        return 0
