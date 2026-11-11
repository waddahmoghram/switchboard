"""Read OFX 1.x (SGML), OFX 2.x (XML), and QFX statement files.

Format quirks handled here, per the OFX 2.3 specification (Financial Data Exchange, 2020):

* OFX 1.x files start with plain ``KEY:VALUE`` header lines (``OFXHEADER:100``, ``DATA:OFXSGML``,
  ``ENCODING:USASCII``, ``CHARSET:1252``, ...) followed by SGML in which data elements usually have no
  end tag: ``<TRNAMT>-80.32`` ends where the next tag starts. Aggregates such as ``<STMTTRN>`` are closed.
* OFX 2.x files start with ``<?xml ...?>`` and ``<?OFX OFXHEADER="200" ...?>`` and close every element.
* QFX is Quicken's variant: OFX plus Intuit-specific elements such as ``<INTU.BID>``, which are ignored.
* Bank statements live in ``BANKMSGSRSV1/STMTTRNRS/STMTRS`` and credit card statements in
  ``CREDITCARDMSGSRSV1/CCSTMTTRNRS/CCSTMTRS``; both list ``STMTTRN`` aggregates in ``BANKTRANLIST``.
* Dates look like ``20261105120000.000[-8:PST]``; fields may be cut from the right (``20261105``).
* Amounts use ``.`` or ``,`` as the decimal mark and never group thousands.
* ``TRNAMT`` is signed from the account holder's point of view: a card purchase or a checking debit is
  negative, and a card payment is positive.

One tolerant tokenizer reads both syntaxes, so bank files never reach an XML parser: there is no DTD,
entity expansion, or external resource to worry about, and the slightly malformed files banks produce
(an unescaped ``&``, an encoding label Python does not know) still parse.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

from switchboard.connectors.finance.models import Balance, FormatError, ParsedTransaction
from switchboard.connectors.finance.money import parse_ofx_amount

_MAX_DEPTH = 64
# A start or end tag, an optional self-closing slash, and the text up to the next tag.
_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9._]*)[^<>]*?(/?)>([^<]*)")
_MARKUP = {"<!--": "-->", "<?": "?>"}  # comments and processing instructions, removed before parsing
_NOT_CHARSETS = frozenset({"idna", "punycode"})  # domain-name codecs; punycode decoding is quadratic
_OFX_START = re.compile(r"<OFX(?:\s[^>]*)?>", re.IGNORECASE)
_ENTITY = re.compile(r"&(#\d+|#[xX][0-9A-Fa-f]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_XML_ENCODING = re.compile(r"""<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""", re.IGNORECASE)
_HEADER_LINE = re.compile(r"^\s*([A-Z]+)\s*:\s*(\S*)\s*$", re.MULTILINE)
_OFX_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})(?:\d{2,6}(?:\.\d+)?)?\s*(?:\[[^\]]*\])?")


@dataclass
class Node:
    """An element of the OFX tree: a leaf with ``text`` or an aggregate with ``children``."""

    tag: str
    text: str | None = None
    children: list[Node] = field(default_factory=list)

    def child(self, tag: str) -> Node | None:
        """The first direct child named ``tag``."""
        return next((node for node in self.children if node.tag == tag), None)

    def iter(self, tag: str) -> Iterator[Node]:
        """Every descendant named ``tag``, depth first."""
        for node in self.children:
            if node.tag == tag:
                yield node
            yield from node.iter(tag)

    def find_text(self, tag: str) -> str | None:
        """Text of the first descendant leaf named ``tag``, depth first."""
        return next((node.text for node in self.iter(tag) if node.text is not None), None)


@dataclass(frozen=True)
class OfxStatement:
    """One account's statement. The account number is excluded from ``repr`` so it never reaches logs."""

    kind: str
    account_type: str
    account_number: str = field(repr=False)
    bank_id: str = field(repr=False)
    currency: str
    transactions: tuple[ParsedTransaction, ...]
    ledger_balance: Balance | None
    available_balance: Balance | None


def is_ofx(raw: bytes) -> bool:
    """True when the start of the file looks like OFX or QFX."""
    head = raw[:4096].upper()
    return b"OFXHEADER" in head or b"<OFX>" in head


def parse_ofx(raw: bytes) -> list[OfxStatement]:
    """Every bank and credit card statement in an OFX or QFX file."""
    text = decode_ofx(raw)
    start = _OFX_START.search(text)
    if start is None:
        raise FormatError("not an OFX file: there is no <OFX> element")
    root = build_tree(strip_markup(text[start.start() :]))
    statements = [_statement(node, "bank", "BANKACCTFROM") for node in root.iter("STMTRS")]
    statements += [_statement(node, "credit_card", "CCACCTFROM") for node in root.iter("CCSTMTRS")]
    if not statements:
        raise FormatError("the file has no bank or credit card statement (STMTRS or CCSTMTRS)")
    return statements


def decode_ofx(raw: bytes) -> str:
    """Decode with the encoding the file declares; undecodable bytes become U+FFFD."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    return raw.decode(_codec_for(raw[:4096].decode("latin-1")), errors="replace")


def _codec_for(head: str) -> str:
    """Pick the codec from the XML declaration, or from the OFX 1.x ENCODING and CHARSET headers.

    ``CHARSET:1252`` means Windows code page 1252. ``CHARSET:NONE`` and a missing header also use
    cp1252, a superset of US-ASCII that decodes the accented letters many banks send anyway. An XML
    declaration naming an encoding Python does not know (``USASCII``) falls back to UTF-8.
    """
    declared = _XML_ENCODING.search(head)
    if declared is not None:
        return _codec_name(declared.group(1)) or "utf-8"
    if head.lstrip().startswith("<?xml"):
        return "utf-8"
    headers = {key: value.upper() for key, value in _HEADER_LINE.findall(head)}
    if headers.get("ENCODING") in {"UTF-8", "UTF8"}:
        return "utf-8"
    if headers.get("CHARSET") in {"ISO-8859-1", "8859-1"}:
        return "latin-1"
    return "cp1252"


def _codec_name(label: str) -> str | None:
    """Python's name for a character set, or None when the label is unknown or names something else.

    ``base64``, ``hex``, and ``zlib`` transform bytes and fail in ``bytes.decode``; ``idna`` cannot replace
    bad input and ``punycode`` takes quadratic time, so neither may decode a whole statement.
    """
    try:
        name = codecs.lookup(label).name
        b"a".decode(name, "replace")  # not b"": bytes.decode returns "" for empty input without the codec
    except (LookupError, UnicodeError):
        return None
    return None if name in _NOT_CHARSETS else name


def strip_markup(text: str) -> str:
    """Remove ``<!-- comments -->`` and ``<?processing instructions?>`` in one forward pass.

    The result equals substituting ``<!--.*?-->|<\\?.*?\\?>`` with nothing, but a lazy regular expression
    rescans to the end of the text for every opener that is never closed, which is quadratic. Here each
    search resumes where the previous one stopped, and an opener with no closer after it means no later
    opener of that kind can be closed either.
    """
    upcoming = {opener: text.find(opener) for opener in _MARKUP}
    parts: list[str] = []
    position = 0
    while True:
        for opener, found in list(upcoming.items()):
            if 0 <= found < position:
                upcoming[opener] = text.find(opener, position)
        live = [(found, opener) for opener, found in upcoming.items() if found >= 0]
        if not live:
            break
        start, opener = min(live)
        closer = _MARKUP[opener]
        end = text.find(closer, start + len(opener))
        if end < 0:
            upcoming[opener] = -1
            continue
        parts.append(text[position:start])
        position = end + len(closer)
    parts.append(text[position:])
    return "".join(parts)


def build_tree(text: str) -> Node:
    """Build a tree from SGML or XML OFX markup.

    A start tag followed by text is a leaf, whether or not an end tag follows. A start tag followed by
    another tag is an aggregate when its name appears as an end tag somewhere in the file; otherwise it
    is an empty SGML leaf (``<MEMO>`` with nothing after it) and must not swallow its siblings.
    """
    tokens = [
        (match.group(1) == "/", match.group(2).upper(), match.group(3) == "/", match.group(4))
        for match in _TOKEN.finditer(text)
    ]
    closed = {tag for is_end, tag, _, _ in tokens if is_end}
    root = Node("")
    stack = [root]
    for is_end, tag, self_closing, trailing in tokens:
        if is_end:
            _close(stack, tag)
            continue
        node = Node(tag)
        stack[-1].children.append(node)
        value = trailing.strip()
        if value:
            node.text = _unescape(value)
        elif tag in closed and not self_closing:
            if len(stack) > _MAX_DEPTH:
                raise FormatError("OFX elements are nested too deeply")
            stack.append(node)
    return root


def _close(stack: list[Node], tag: str) -> None:
    """Close the innermost open aggregate named ``tag``; end tags of leaves are ignored."""
    for depth in range(len(stack) - 1, 0, -1):
        if stack[depth].tag == tag:
            del stack[depth:]
            return


def _unescape(text: str) -> str:
    """Replace the five XML entities and numeric character references; leave anything else alone."""
    return _ENTITY.sub(_entity, text)


def _entity(match: re.Match[str]) -> str:
    name = match.group(1)
    if not name.startswith("#"):
        return _NAMED[name]
    code = int(name[2:], 16) if name[1:2] in {"x", "X"} else int(name[1:])
    if 0 < code < 0x110000 and not 0xD800 <= code <= 0xDFFF:
        return chr(code)
    return "\ufffd"


def parse_ofx_date(value: str) -> date:
    """The calendar date of an OFX date-time, as the bank wrote it.

    ``20261105120000.000[-8:PST]`` is 2026-11-05. The time and the bracketed zone are checked for shape
    but not applied: converting to UTC would move a late-evening purchase to the next day, which is not
    the date printed on the statement.
    """
    match = _OFX_DATE.fullmatch(value.strip())
    if match is None:
        raise FormatError("a date is not in the OFX form YYYYMMDD[HHMMSS[.XXX]][[offset:zone]]")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise FormatError("a date has an impossible month or day") from exc


def _statement(node: Node, kind: str, account_tag: str) -> OfxStatement:
    account = node.child(account_tag)
    number = account.find_text("ACCTID") if account is not None else None
    if account is None or not number:
        raise FormatError(f"a {node.tag} statement has no {account_tag}/ACCTID")
    tranlist = node.child("BANKTRANLIST")
    transactions = () if tranlist is None else tuple(_transaction(item) for item in tranlist.iter("STMTTRN"))
    default_type = "CREDITCARD" if kind == "credit_card" else "OTHER"
    return OfxStatement(
        kind=kind,
        account_type=(account.find_text("ACCTTYPE") or default_type).upper(),
        account_number=number,
        bank_id=account.find_text("BANKID") or "",
        currency=(node.find_text("CURDEF") or "").upper(),
        transactions=transactions,
        ledger_balance=_balance(node.child("LEDGERBAL")),
        available_balance=_balance(node.child("AVAILBAL")),
    )


def _transaction(node: Node) -> ParsedTransaction:
    posted = node.find_text("DTPOSTED")
    amount = node.find_text("TRNAMT")
    if posted is None or amount is None:
        raise FormatError("a transaction (STMTTRN) is missing DTPOSTED or TRNAMT")
    try:
        cents = parse_ofx_amount(amount)
    except ValueError as exc:
        raise FormatError("a transaction has an unreadable TRNAMT") from exc
    # NAME is limited to 32 characters; PAYEE/NAME and EXTDNAME are the longer alternatives.
    name = node.find_text("NAME") or node.find_text("EXTDNAME") or ""
    memo = node.find_text("MEMO") or ""
    kind = (node.find_text("TRNTYPE") or "").upper()
    return ParsedTransaction(
        posted=parse_ofx_date(posted),
        amount_cents=cents,
        description=name or memo or kind or "transaction",
        memo=memo if name else "",
        external_id=node.find_text("FITID"),
        check_number=node.find_text("CHECKNUM") or "",
        kind=kind,
    )


def _balance(node: Node | None) -> Balance | None:
    if node is None:
        return None
    amount = node.find_text("BALAMT")
    if amount is None:
        return None
    try:
        cents = parse_ofx_amount(amount)
    except ValueError as exc:
        raise FormatError("a balance has an unreadable BALAMT") from exc
    as_of = node.find_text("DTASOF")
    return Balance(cents, parse_ofx_date(as_of) if as_of else None)
