"""Parse a bounded numeric-date subset of Android and iPhone personal chat exports."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime

from switchboard.connectors._archive import (
    MAX_IMPORT_BYTES,
    MAX_RECORDS,
    MAX_TEXT_CHARS,
    ArchiveRecord,
    ImportFormatError,
    decode_text,
)
from switchboard.connectors.whatsapp.settings import DATE_ORDERS
from switchboard.util import sha256_hex

PARSER_VERSION = "numeric-v1"
MAX_SENDER_CHARS = 200
_PREFIX_CHARS = " \t\u00a0\u202f\u200e"
_SPACE = r"[ \t\u00a0\u202f]"
_GAP = r"[ \t\u00a0\u202f\u200e]"
_DATE = r"[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4}"
_TIME = rf"[0-9]{{1,2}}:[0-9]{{2}}(?::[0-9]{{2}})?(?:{_SPACE}+(?:AM|PM))?"
_ANDROID = re.compile(
    rf"(?P<date>{_DATE}),{_GAP}*(?P<time>{_TIME}){_GAP}+-{_SPACE}+(?P<payload>.+)",
    re.IGNORECASE | re.ASCII,
)
_IPHONE = re.compile(
    rf"\[{_GAP}*(?P<date>{_DATE}),{_GAP}*(?P<time>{_TIME}){_GAP}*\]{_SPACE}+(?P<payload>.+)",
    re.IGNORECASE | re.ASCII,
)
_DATE_COMPONENTS = re.compile(
    r"(?P<first>[0-9]{1,4})(?P<separator>[/.-])(?P<second>[0-9]{1,2})"
    r"(?P=separator)(?P<third>[0-9]{1,4})"
)
_CLOCK = re.compile(
    rf"(?P<hour>[0-9]{{1,2}}):(?P<minute>[0-9]{{2}})(?::(?P<second>[0-9]{{2}}))?"
    rf"(?:{_SPACE}+(?P<period>AM|PM))?",
    re.IGNORECASE | re.ASCII,
)
_HEADER_LIKE = re.compile(
    rf"(?:\[{_GAP}*)?\d{{1,4}}"
    rf"(?:[/.-][^\s/.\-\[\]:,]{{1,24}}[/.-]|{_SPACE}+[^\s,\[\]:]{{1,24}}{_SPACE}+\d{{2,4}})"
)
_DATE_INDEXES = {"MDY": (2, 0, 1), "DMY": (2, 1, 0), "YMD": (0, 1, 2)}


class _Body:
    def __init__(self, initial: str) -> None:
        self.parts: list[str] = []
        self.characters = 0
        self._hash = hashlib.sha256()
        self.append(initial, continuation=False)

    def append(self, line: str, *, continuation: bool = True) -> None:
        chunk = f"\n{line}" if continuation else line
        if self.characters < MAX_TEXT_CHARS:
            self.parts.append(chunk[: MAX_TEXT_CHARS - self.characters])
        self.characters += len(chunk)
        self._hash.update(chunk.encode("utf-8"))

    def digest(self) -> str:
        return self._hash.hexdigest()


@dataclass
class _Message:
    timestamp: str
    sender: str | None
    body: _Body


def _timestamp(date: str, clock: str, date_order: str, line_number: int) -> str:
    date_match = _DATE_COMPONENTS.fullmatch(date)
    time_match = _CLOCK.fullmatch(clock)
    problem = f"invalid numeric date or time at line {line_number}; check date_order"
    if date_match is None or time_match is None:
        raise ImportFormatError(problem)
    parts = [date_match["first"], date_match["second"], date_match["third"]]
    year_index, month_index, day_index = _DATE_INDEXES[date_order]
    if len(parts[year_index]) not in {2, 4} or any(
        len(parts[index]) > 2 for index in (month_index, day_index)
    ):
        raise ImportFormatError(problem)
    year = int(parts[year_index])
    if len(parts[year_index]) == 2:
        year += 2000
    hour = int(time_match["hour"])
    period = time_match["period"]
    if period is not None:
        if not 1 <= hour <= 12:
            raise ImportFormatError(problem)
        hour = hour % 12 + (12 if period.upper() == "PM" else 0)
    try:
        timestamp = datetime(
            year,
            int(parts[month_index]),
            int(parts[day_index]),
            hour,
            int(time_match["minute"]),
            int(time_match["second"] or 0),
        )
    except ValueError as exc:
        raise ImportFormatError(problem) from exc
    return timestamp.isoformat(timespec="seconds")


def _record(message: _Message, chat_label: str, timezone: str | None) -> ArchiveRecord:
    sender = message.sender
    return ArchiveRecord(
        "message" if sender is not None else "system_notice",
        chat_label,
        {
            "timestamp_local": message.timestamp,
            "timestamp_kind": "local_wall_clock",
            "timestamp_has_offset": False,
            "timezone_context": timezone,
            "sender": sender[:MAX_SENDER_CHARS] if sender is not None else None,
            "sender_is_heuristic": True,
            "sender_truncated": sender is not None and len(sender) > MAX_SENDER_CHARS,
            "sender_sha256": sha256_hex(sender) if sender is not None else None,
            "kind_is_heuristic": True,
            "text": "".join(message.body.parts),
            "text_chars": message.body.characters,
            "text_truncated": message.body.characters > MAX_TEXT_CHARS,
            "text_sha256": message.body.digest(),
        },
    )


def parse_chat(
    raw: bytes, *, chat_label: str, date_order: str, timezone: str | None = None
) -> list[ArchiveRecord]:
    """Read historical text, retaining local timestamps and treating all names as unverified text."""
    if date_order not in DATE_ORDERS:
        raise ImportFormatError("date_order must explicitly select MDY, DMY, or YMD")
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportFormatError(f"exports must not exceed {MAX_IMPORT_BYTES} bytes")
    records: list[ArchiveRecord] = []
    current: _Message | None = None
    with io.StringIO(decode_text(raw), newline=None) as lines:
        for line_number, raw_line in enumerate(lines, 1):
            line = raw_line.removesuffix("\n")
            candidate = line.lstrip(_PREFIX_CHARS)
            match = _ANDROID.fullmatch(candidate) or _IPHONE.fullmatch(candidate)
            if match is not None:
                if current is not None:
                    records.append(_record(current, chat_label, timezone))
                if len(records) >= MAX_RECORDS:
                    raise ImportFormatError(
                        f"exports may contain at most {MAX_RECORDS} records (line {line_number})"
                    )
                timestamp = _timestamp(match["date"], match["time"], date_order, line_number)
                name, separator, initial = match["payload"].partition(": ")
                sender = name if separator and name else None
                if sender is None:
                    initial = match["payload"]
                current = _Message(timestamp, sender, _Body(initial))
            elif _HEADER_LIKE.match(candidate) or (current is None and candidate):
                raise ImportFormatError(f"unsupported or malformed record header at line {line_number}")
            elif current is not None:
                current.body.append(line)
    if current is not None:
        records.append(_record(current, chat_label, timezone))
    if not records:
        raise ImportFormatError("the export contains no supported records")
    return records
