"""A small RFC 5545 (iCalendar) reader and writer for events.

Reading covers what an agenda needs: content-line unfolding (§3.1), parameters with quoted values,
TEXT escapes (§3.3.11), DATE and DATE-TIME values (§3.3.4, §3.3.5), DURATION (§3.3.6), and the VEVENT
properties listed on :class:`Event`. ``VTIMEZONE`` blocks are ignored: ``TZID`` values are looked up as
IANA names with :mod:`zoneinfo`, which matches what Google, iCloud, Fastmail, and most CalDAV servers
write. Unknown names (for example Windows names from Outlook) fall back to the calendar's configured
zone and add a warning. Floating times and all-day dates use the calendar's zone too.

Writing produces CRLF line endings and folds lines at 75 octets without splitting UTF-8 sequences.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from switchboard.util import iso, sha256_hex, truncate

PRODUCT_ID = "-//switchboard//calendar connector//EN"
STORED_TEXT_LIMIT = 4000
MAX_WARNINGS = 20

_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATE_TIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_UTC_NAMES = {"UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT"}


# ---- content lines -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Property:
    """One content line. ``name`` and parameter names are upper-case; ``value`` is still escaped."""

    name: str
    params: Mapping[str, str]
    value: str


@dataclass
class Component:
    """A ``BEGIN:X`` … ``END:X`` block with its own properties and nested components."""

    name: str
    properties: list[Property] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)

    def first(self, name: str) -> Property | None:
        return next((prop for prop in self.properties if prop.name == name), None)

    def all(self, name: str) -> list[Property]:
        return [prop for prop in self.properties if prop.name == name]

    def text(self, name: str) -> str:
        prop = self.first(name)
        return "" if prop is None else unescape_text(prop.value)


def unfold(text: str) -> list[str]:
    """Split into logical lines: a line break followed by one space or tab continues the line."""
    lines: list[str] = []
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _scan(line: str, index: int, stops: str) -> int:
    while index < len(line) and line[index] not in stops:
        index += 1
    return index


def _parse_param(line: str, index: int) -> tuple[str, list[str], int]:
    """Parse ``NAME=value[,value]`` starting at ``index``; quoted values may hold ``:``, ``;``, and ``,``."""
    end = _scan(line, index, "=;:")
    name = line[index:end].strip().upper()
    if end >= len(line) or line[end] != "=":
        return name, [], end
    values: list[str] = []
    index = end + 1
    while True:
        if index < len(line) and line[index] == '"':
            close = line.find('"', index + 1)
            if close == -1:
                raise ValueError("unterminated quoted parameter value")
            values.append(line[index + 1 : close])
            index = close + 1
        else:
            stop = _scan(line, index, ",;:")
            values.append(line[index:stop])
            index = stop
        if index < len(line) and line[index] == ",":
            index += 1
            continue
        return name, values, index


def parse_line(line: str) -> Property:
    """Split ``NAME;PARAM=VALUE;…:VALUE`` at the first colon outside a quoted parameter value."""
    index = _scan(line, 0, ";:")
    name = line[:index].strip().upper()
    if not name or index >= len(line):
        raise ValueError("not a content line")
    params: dict[str, str] = {}
    while line[index] == ";":
        param, values, index = _parse_param(line, index + 1)
        if index >= len(line) or line[index] not in ";:":
            raise ValueError("malformed parameter")
        if param:
            joined = ",".join(values)
            params[param] = f"{params[param]},{joined}" if param in params else joined
    return Property(name, params, line[index + 1 :])


def parse_components(text: str) -> tuple[list[Component], list[str]]:
    """Build the component tree. Malformed lines are skipped; a missing END closes at end of input."""
    roots: list[Component] = []
    stack: list[Component] = []
    warnings: list[str] = []
    for line in unfold(text):
        try:
            prop = parse_line(line)
        except ValueError:
            if len(warnings) < MAX_WARNINGS:
                warnings.append("skipped a malformed line")
            continue
        if prop.name == "BEGIN":
            component = Component(prop.value.strip().upper())
            (stack[-1].components if stack else roots).append(component)
            stack.append(component)
        elif prop.name == "END":
            name = prop.value.strip().upper()
            depth = next((i for i in range(len(stack) - 1, -1, -1) if stack[i].name == name), None)
            if depth is not None:
                del stack[depth:]
        elif stack:
            stack[-1].properties.append(prop)
    return roots, warnings


def unescape_text(value: str) -> str:
    """Undo TEXT escaping: ``\\n`` or ``\\N`` is a newline; ``\\,``, ``\\;``, and ``\\\\`` are literal."""
    return _ESCAPE.sub(lambda match: "\n" if match.group(1) in "nN" else match.group(1), value)


def escape_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str, limit: int = 75) -> str:
    """Fold one content line so no physical line exceeds ``limit`` octets (RFC 5545 §3.1).

    Continuation lines start with a space, which counts toward their length. Characters are never
    split, so a multi-octet UTF-8 sequence always stays on one physical line.
    """
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    width = limit
    for char in line:
        octets = len(char.encode("utf-8"))
        if size + octets > width:
            pieces.append("".join(current))
            current, size, width = [], 0, limit - 1
        current.append(char)
        size += octets
    pieces.append("".join(current))
    return "\r\n ".join(pieces)


# ---- time zones and values ------------------------------------------------------------------------


def zone_named(name: str) -> tzinfo | None:
    """Resolve an IANA zone name; None when unknown.

    Vendor-prefixed names such as ``/mozilla.org/20050126_1/America/New_York`` are retried with their
    last three and last two path segments.
    """
    cleaned = name.strip().strip('"')
    if cleaned.upper() in _UTC_NAMES:
        return UTC
    parts = [part for part in cleaned.split("/") if part]
    candidates = [cleaned, *("/".join(parts[-count:]) for count in (3, 2) if len(parts) > count)]
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
            continue
    return None


def zone_key(zone: tzinfo) -> str:
    """The IANA name used to store a zone; ``UTC`` for :data:`datetime.UTC`."""
    key = getattr(zone, "key", None)
    return key if isinstance(key, str) else "UTC"


def parse_value(value: str) -> tuple[datetime, bool, bool]:
    """Parse DATE or DATE-TIME text into ``(naive datetime, is_date, is_utc)``."""
    text = value.strip().upper()
    match = _DATE.match(text)
    if match:
        return datetime(int(match[1]), int(match[2]), int(match[3])), True, False
    match = _DATE_TIME.match(text)
    if match is None:
        raise ValueError(f"not a DATE or DATE-TIME value: {truncate(value.strip() or '-', 40)!r}")
    second = min(int(match[6]), 59)  # RFC 5545 allows 60 for a leap second; datetime does not
    moment = datetime(int(match[1]), int(match[2]), int(match[3]), int(match[4]), int(match[5]), second)
    return moment, False, match[7] == "Z"


def format_date(day: date) -> str:
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def format_utc(moment: datetime) -> str:
    """A DATE-TIME in UTC form, such as ``20261112T140000Z``."""
    utc = moment.astimezone(UTC)
    return f"{format_date(utc.date())}T{utc.hour:02d}{utc.minute:02d}{utc.second:02d}Z"


@dataclass(frozen=True)
class Duration:
    """A DURATION value: ``days`` are nominal wall-clock days, ``seconds`` are exact (RFC 5545 §3.3.6)."""

    days: int
    seconds: int


def parse_duration(value: str) -> Duration:
    match = _DURATION.match(value.strip().upper())
    if match is None or not any(match.groups()[1:]):
        raise ValueError(f"not a DURATION value: {truncate(value.strip() or '-', 40)!r}")
    sign = -1 if match[1] == "-" else 1
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups()[1:])
    return Duration(sign * (weeks * 7 + days), sign * (hours * 3600 + minutes * 60 + seconds))


def add_duration(start: datetime, duration: Duration) -> datetime:
    """Add nominal days on the wall clock, then exact seconds on the UTC time line.

    A one-day duration from 09:00 ends at 09:00 the next day even across a daylight-saving change,
    while ``PT24H`` ends 24 real hours later.
    """
    nominal = start + timedelta(days=duration.days)
    return (nominal.astimezone(UTC) + timedelta(seconds=duration.seconds)).astimezone(start.tzinfo)


# ---- events ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One VEVENT. ``start`` and ``end`` are aware; ``start.tzinfo`` is the zone recurrences expand in.

    All-day events start at midnight in the calendar's zone. ``exdates`` are excluded instants;
    ``exdays`` are DATE-valued exclusions, matched by local date.
    """

    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    description: str = ""
    location: str = ""
    status: str = ""
    transparent: bool = False
    organizer: str = ""
    attendees: tuple[str, ...] = ()
    rrule: str = ""
    exdates: tuple[datetime, ...] = ()
    exdays: tuple[date, ...] = ()
    recurrence_id: datetime | None = None

    @property
    def zone(self) -> tzinfo:
        return self.start.tzinfo or UTC

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "zone": zone_key(self.zone),
            "start": self.start.replace(tzinfo=None).isoformat(),
            "end": iso(self.end),
            "all_day": self.all_day,
            "description": self.description,
            "location": self.location,
            "status": self.status,
            "transparent": self.transparent,
            "organizer": self.organizer,
            "attendees": list(self.attendees),
            "rrule": self.rrule,
            "exdates": [iso(moment) for moment in self.exdates],
            "exdays": [day.isoformat() for day in self.exdays],
            "recurrence_id": None if self.recurrence_id is None else iso(self.recurrence_id),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Event:
        zone = zone_named(str(data["zone"])) or UTC
        recurrence_id = data.get("recurrence_id")
        return cls(
            uid=str(data["uid"]),
            summary=str(data["summary"]),
            start=datetime.fromisoformat(str(data["start"])).replace(tzinfo=zone),
            end=datetime.fromisoformat(str(data["end"])).astimezone(zone),
            all_day=bool(data["all_day"]),
            description=str(data.get("description", "")),
            location=str(data.get("location", "")),
            status=str(data.get("status", "")),
            transparent=bool(data.get("transparent", False)),
            organizer=str(data.get("organizer", "")),
            attendees=tuple(str(item) for item in data.get("attendees", [])),
            rrule=str(data.get("rrule", "")),
            exdates=tuple(datetime.fromisoformat(str(item)) for item in data.get("exdates", [])),
            exdays=tuple(date.fromisoformat(str(item)) for item in data.get("exdays", [])),
            recurrence_id=None if recurrence_id is None else datetime.fromisoformat(str(recurrence_id)),
        )


@dataclass(frozen=True)
class Calendar:
    events: list[Event]
    warnings: list[str]
    name: str = ""


class _Reader:
    """Per-calendar parsing state: the fallback zone, resolved TZIDs, and warnings."""

    def __init__(self, zone: tzinfo) -> None:
        self.zone = zone
        self.warnings: list[str] = []
        self._zones: dict[str, tzinfo] = {}

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)

    def zone_for(self, tzid: str | None) -> tzinfo:
        if not tzid:
            return self.zone
        if tzid not in self._zones:
            found = zone_named(tzid)
            if found is None:
                self.warn(f"unknown time zone {truncate(tzid, 64)!r}; used {zone_key(self.zone)} instead")
                found = self.zone
            self._zones[tzid] = found
        return self._zones[tzid]

    def moments(self, prop: Property) -> Iterator[tuple[datetime, bool]]:
        """Each comma-separated value of a DATE or DATE-TIME property as ``(aware datetime, is_date)``."""
        for value in prop.value.split(","):
            naive, is_date, is_utc = parse_value(value)
            if is_date:
                yield naive.replace(tzinfo=self.zone), True
            elif is_utc:
                yield naive.replace(tzinfo=UTC), False
            else:
                yield naive.replace(tzinfo=self.zone_for(prop.params.get("TZID"))), False

    def moment(self, prop: Property) -> tuple[datetime, bool]:
        return next(self.moments(prop))


def _address(value: str) -> str:
    text = value.strip()
    return text[7:] if text.lower().startswith("mailto:") else text


def _end(component: Component, reader: _Reader, start: datetime, all_day: bool) -> datetime:
    """DTEND, else DTSTART + DURATION, else the RFC 5545 §3.6.1 default (one day, or zero length)."""
    end_prop = component.first("DTEND")
    duration_prop = component.first("DURATION")
    if end_prop is not None:
        end = reader.moment(end_prop)[0]
    elif duration_prop is not None:
        end = add_duration(start, parse_duration(duration_prop.value))
    else:
        end = start + timedelta(days=1) if all_day else start
    if all_day and end.astimezone(UTC) <= start.astimezone(UTC):
        return start + timedelta(days=1)
    if end.astimezone(UTC) < start.astimezone(UTC):
        reader.warn("an event ended before it started; treated it as zero length")
        return start
    return end


def _exclusions(component: Component, reader: _Reader) -> tuple[tuple[datetime, ...], tuple[date, ...]]:
    moments: list[datetime] = []
    days: list[date] = []
    for prop in component.all("EXDATE"):
        for moment, is_date in reader.moments(prop):
            if is_date:
                days.append(moment.date())
            else:
                moments.append(moment.astimezone(UTC))
    return tuple(moments), tuple(days)


def _event(component: Component, reader: _Reader) -> Event | None:
    start_prop = component.first("DTSTART")
    if start_prop is None:
        reader.warn("skipped an event without DTSTART")
        return None
    start, all_day = reader.moment(start_prop)
    summary = truncate(component.text("SUMMARY"), 500)
    uid = component.text("UID").strip() or "generated-" + sha256_hex(f"{start_prop.value}|{summary}")[:16]
    rules = component.all("RRULE")
    if len(rules) > 1:
        reader.warn("an event has several RRULE lines; only the first was used")
    if component.first("RDATE") is not None:
        reader.warn("RDATE is not supported; extra dates were ignored")
    exdates, exdays = _exclusions(component, reader)
    organizer = component.first("ORGANIZER")
    recurrence = component.first("RECURRENCE-ID")
    return Event(
        uid=uid,
        summary=summary,
        start=start,
        end=_end(component, reader, start, all_day),
        all_day=all_day,
        description=truncate(component.text("DESCRIPTION"), STORED_TEXT_LIMIT),
        location=truncate(component.text("LOCATION"), 500),
        status=component.text("STATUS").strip().upper(),
        transparent=component.text("TRANSP").strip().upper() == "TRANSPARENT",
        organizer="" if organizer is None else _address(organizer.value),
        attendees=tuple(_address(prop.value) for prop in component.all("ATTENDEE")[:100]),
        rrule=rules[0].value.strip() if rules else "",
        exdates=exdates,
        exdays=exdays,
        recurrence_id=None if recurrence is None else reader.moment(recurrence)[0],
    )


def parse_calendar(text: str, zone: tzinfo) -> Calendar:
    """Read every VEVENT of an iCalendar stream; events that cannot be read are skipped with a warning."""
    roots, structural = parse_components(text)
    reader = _Reader(zone)
    for message in structural:
        reader.warn(message)
    events: list[Event] = []
    name = ""
    for root in roots:
        if root.name != "VCALENDAR":
            continue
        name = name or truncate(root.text("X-WR-CALNAME"), 100)
        for child in root.components:
            if child.name != "VEVENT":
                continue
            try:
                event = _event(child, reader)
            except (ValueError, OverflowError) as exc:
                reader.warn(f"skipped an event: {exc}")
                continue
            if event is not None:
                events.append(event)
    return Calendar(events, reader.warnings, name)


# ---- writing --------------------------------------------------------------------------------------


def _times(event: Event) -> list[str]:
    if event.all_day:
        last = event.end.astimezone(event.zone).date()
        return [
            f"DTSTART;VALUE=DATE:{format_date(event.start.date())}",
            f"DTEND;VALUE=DATE:{format_date(last)}",
        ]
    return [f"DTSTART:{format_utc(event.start)}", f"DTEND:{format_utc(event.end)}"]


def _mailto(address: str) -> str:
    return "mailto:" + address.replace("\r", "").replace("\n", "")


def serialize(event: Event, *, stamp: datetime, method: str | None = None) -> str:
    """One VCALENDAR holding ``event``, with CRLF line endings and folded lines.

    Times are written in UTC, or as DATEs for all-day events, so no VTIMEZONE block is needed.
    CalDAV resources must not carry METHOD (RFC 4791 §4.1) and iMIP bodies must (RFC 6047 §2.4), so
    the caller chooses.
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODUCT_ID}", "CALSCALE:GREGORIAN"]
    if method:
        lines.append(f"METHOD:{method}")
    lines += [
        "BEGIN:VEVENT",
        f"UID:{escape_text(event.uid)}",
        f"DTSTAMP:{format_utc(stamp)}",
        *_times(event),
        "SEQUENCE:0",
        f"SUMMARY:{escape_text(event.summary)}",
    ]
    optional = (("LOCATION", event.location), ("DESCRIPTION", event.description))
    lines += [f"{name}:{escape_text(value)}" for name, value in optional if value]
    if event.status:
        lines.append(f"STATUS:{event.status}")
    if event.organizer:
        lines.append(f"ORGANIZER:{_mailto(event.organizer)}")
    lines += [
        f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:{_mailto(address)}"
        for address in event.attendees
    ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "".join(fold(line) + "\r\n" for line in lines)
