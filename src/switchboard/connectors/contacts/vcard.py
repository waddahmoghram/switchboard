"""A tolerant vCard reader: RFC 6350 (vCard 4.0), plus RFC 2426 (vCard 3.0) as Google and Apple export it.

Reads FN, N, EMAIL and TEL with their TYPE parameters, ORG, BDAY, NOTE (capped), and UID. Lines are
unfolded (RFC 6350 §3.2), property groups such as Apple's ``item1.EMAIL`` are ignored, double-quoted
parameter values are honoured, TEXT escapes are undone (§3.4), and vCard 2.1 bare parameters
(``EMAIL;INTERNET;HOME:``) count as types. vCard 2.1 quoted-printable values are not decoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from switchboard.util import sha256_hex, truncate

NOTE_LIMIT = 500
MAX_WARNINGS = 20
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_IGNORED_TYPES = {"internet", "x400"}


@dataclass(frozen=True)
class Entry:
    """An email address or phone number with its lower-case TYPE values, such as ``("work", "pref")``."""

    value: str
    types: tuple[str, ...] = ()


@dataclass(frozen=True)
class Card:
    uid: str
    fn: str
    emails: tuple[Entry, ...] = ()
    phones: tuple[Entry, ...] = ()
    org: str = ""
    bday: str = ""
    note: str = ""


@dataclass(frozen=True)
class _Line:
    name: str
    params: dict[str, list[str]]
    value: str


def unfold(text: str) -> list[str]:
    """Logical lines: a line break followed by one space or tab continues the previous line."""
    lines: list[str] = []
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    return _ESCAPE.sub(lambda match: "\n" if match.group(1) in "nN" else match.group(1), value)


def _split_unquoted(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside double quotes; quotes stay in the pieces."""
    pieces: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted:
            pieces.append("".join(current))
            current = []
        else:
            current.append(char)
    pieces.append("".join(current))
    return pieces


def _value_start(line: str) -> int:
    """Index of the first colon outside a quoted parameter value, or -1."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return index
    return -1


def parse_line(line: str) -> _Line | None:
    """Split ``[group.]NAME;PARAM=VALUE…:VALUE``; None when the line has no value separator."""
    colon = _value_start(line)
    if colon <= 0:
        return None
    head = _split_unquoted(line[:colon], ";")
    name = head[0].strip().upper().rsplit(".", 1)[-1]
    params: dict[str, list[str]] = {}
    for piece in head[1:]:
        key, separator, raw = piece.partition("=")
        if not separator:
            params.setdefault("TYPE", []).append(key.strip())
            continue
        values = [value.strip().strip('"') for value in _split_unquoted(raw, ",")]
        params.setdefault(key.strip().upper(), []).extend(value for value in values if value)
    return _Line(name, params, line[colon + 1 :])


def _components(value: str) -> list[str]:
    """Split a structured value (N, ORG) on unescaped semicolons, then unescape each part."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if value[index] == ";":
            parts.append(unescape("".join(current)))
            current = []
        else:
            current.append(value[index])
        index += 1
    parts.append(unescape("".join(current)))
    return parts


def _types(line: _Line) -> tuple[str, ...]:
    found: list[str] = []
    for value in line.params.get("TYPE", []):
        for item in value.split(","):
            kind = item.strip().strip('"').lower()
            if kind and kind not in _IGNORED_TYPES and kind not in found:
                found.append(kind)
    if line.params.get("PREF") and "pref" not in found:
        found.append("pref")
    return tuple(found)


def _display_name(components: list[str]) -> str:
    """N holds family;given;additional;prefixes;suffixes; return them in reading order."""
    padded = [*components, "", "", "", "", ""][:5]
    family, given, additional, prefix, suffix = (" ".join(part.replace(",", " ").split()) for part in padded)
    return " ".join(part for part in (prefix, given, additional, family, suffix) if part)


def _phone(value: str) -> str:
    text = unescape(value).strip()
    return text[4:] if text.lower().startswith("tel:") else text


def _card(lines: list[_Line], raw: str) -> Card:
    names = {"FN": "", "N": "", "ORG": "", "BDAY": "", "NOTE": "", "UID": ""}
    emails: list[Entry] = []
    phones: list[Entry] = []
    for line in lines:
        if line.name == "EMAIL" and unescape(line.value).strip():
            emails.append(Entry(unescape(line.value).strip(), _types(line)))
        elif line.name == "TEL" and _phone(line.value):
            phones.append(Entry(_phone(line.value), _types(line)))
        elif line.name in names and not names[line.name]:
            names[line.name] = line.value
    org = ", ".join(part.strip() for part in _components(names["ORG"]) if part.strip())
    fallback = emails[0].value if emails else phones[0].value if phones else ""
    return Card(
        uid=names["UID"].strip() or "sha256:" + sha256_hex(raw)[:16],
        fn=unescape(names["FN"]).strip() or _display_name(_components(names["N"])) or fallback,
        emails=tuple(emails[:20]),
        phones=tuple(phones[:20]),
        org=truncate(org, 200) if org else "",
        bday=truncate(names["BDAY"].strip(), 32) if names["BDAY"].strip() else "",
        note=truncate(unescape(names["NOTE"]).strip(), NOTE_LIMIT) if names["NOTE"].strip() else "",
    )


def parse_cards(text: str) -> tuple[list[Card], list[str]]:
    """Every ``BEGIN:VCARD`` … ``END:VCARD`` block; unreadable lines and nameless cards add warnings."""
    cards: list[Card] = []
    warnings: list[str] = []

    def warn(message: str) -> None:
        if message not in warnings and len(warnings) < MAX_WARNINGS:
            warnings.append(message)

    current: list[_Line] | None = None
    raw: list[str] = []
    for line in unfold(text):
        parsed = parse_line(line)
        if parsed is None:
            warn("skipped a malformed line")
        elif parsed.name == "BEGIN" and parsed.value.strip().upper() == "VCARD":
            current, raw = [], [line]
        elif parsed.name == "END" and parsed.value.strip().upper() == "VCARD" and current is not None:
            card = _card(current, "\n".join(raw))
            if card.fn:
                cards.append(card)
            else:
                warn("skipped a card without a name, email, or phone")
            current = None
        elif current is not None:
            current.append(parsed)
            raw.append(line)
    if current is not None:
        warn("the data ended inside a vCard")
    return cards, warnings
