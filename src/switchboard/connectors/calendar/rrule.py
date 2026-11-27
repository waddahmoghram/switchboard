"""Expand RFC 5545 recurrence rules (§3.3.10, §3.8.5.3) inside a time window.

Supported: FREQ=DAILY, WEEKLY, MONTHLY, or YEARLY with INTERVAL, COUNT, UNTIL, BYDAY (weekdays, plus
ordinals such as ``2MO`` or ``-1FR`` for MONTHLY and YEARLY), BYMONTHDAY (negative values count from
the end of the month), BYMONTH, and WKST. EXDATEs are removed, and RECURRENCE-ID overrides replace the
instances they name.

Anything else (BYSETPOS, BYWEEKNO, BYYEARDAY, BYHOUR, BYMINUTE, BYSECOND, sub-daily frequencies, or a
malformed rule) adds a warning and yields only the first instance, so an unusual feed never breaks
the agenda. Expansion runs on the wall clock of the event's own zone and converts each instance with
zoneinfo, so a 09:00 meeting stays at 09:00 across daylight-saving changes. A wall time inside a
spring-forward gap uses the offset from before the gap and an ambiguous time takes its first
occurrence, as RFC 5545 §3.3.5 requires (zoneinfo's ``fold=0``).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from switchboard.connectors.calendar.ics import Event, parse_value
from switchboard.util import truncate

MAX_INSTANCES = 1000
MAX_PERIODS = 20_000
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_SUPPORTED = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")


class RuleError(ValueError):
    """The rule is malformed or uses a part this module does not expand."""


@dataclass(frozen=True)
class Rule:
    """A parsed RRULE. ``by_day`` holds ``(ordinal, weekday)`` pairs; ordinal 0 means every such day."""

    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    by_day: tuple[tuple[int, int], ...] = ()
    by_month_day: tuple[int, ...] = ()
    by_month: tuple[int, ...] = ()
    week_start: int = 0


@dataclass(frozen=True)
class Occurrence:
    """One instance of an event inside the requested window."""

    event: Event
    start: datetime
    end: datetime
    recurring: bool


# ---- parsing --------------------------------------------------------------------------------------


def _split(text: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in text.strip().split(";"):
        if not chunk.strip():
            continue
        key, sep, value = chunk.partition("=")
        if not sep:
            raise RuleError(f"malformed RRULE part {truncate(chunk, 20)!r}")
        parts[key.strip().upper()] = value.strip().upper()
    return parts


def _items(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _integer(text: str, low: int, high: int, label: str) -> int:
    try:
        number = int(text)
    except ValueError as exc:
        raise RuleError(f"{label} must be an integer") from exc
    if not low <= number <= high or number == 0:
        raise RuleError(f"{label} value {number} is out of range")
    return number


def _weekday(code: str) -> int:
    if code not in WEEKDAYS:
        raise RuleError(f"unknown weekday {truncate(code, 10)!r}")
    return WEEKDAYS.index(code)


def _by_day(item: str) -> tuple[int, int]:
    match = _BYDAY.match(item)
    if match is None:
        raise RuleError(f"malformed BYDAY value {truncate(item, 10)!r}")
    ordinal = int(match[1]) if match[1] else 0
    if not -53 <= ordinal <= 53:
        raise RuleError(f"BYDAY ordinal {ordinal} is out of range")
    return ordinal, _weekday(match[2])


def _until(text: str, zone: tzinfo) -> datetime:
    """UNTIL is inclusive; a DATE means the end of that day in the event's zone."""
    try:
        naive, is_date, is_utc = parse_value(text)
    except ValueError as exc:
        raise RuleError("UNTIL is not a DATE or DATE-TIME") from exc
    if is_date:
        return datetime.combine(naive.date(), time(23, 59, 59), tzinfo=zone)
    return naive.replace(tzinfo=UTC if is_utc else zone)


def parse_rule(text: str, zone: tzinfo) -> Rule:
    """Parse an RRULE value; raises :class:`RuleError` for anything this module cannot expand."""
    parts = _split(text)
    unknown = sorted(set(parts) - _SUPPORTED)
    if unknown:
        raise RuleError(f"unsupported RRULE part {', '.join(unknown)}")
    freq = parts.get("FREQ", "")
    if freq not in FREQUENCIES:
        raise RuleError(f"unsupported FREQ {freq or '(missing)'}")
    return Rule(
        freq=freq,
        interval=_integer(parts.get("INTERVAL", "1"), 1, 10_000, "INTERVAL"),
        count=_integer(parts["COUNT"], 1, 1_000_000, "COUNT") if "COUNT" in parts else None,
        until=_until(parts["UNTIL"], zone) if "UNTIL" in parts else None,
        by_day=tuple(_by_day(item) for item in _items(parts.get("BYDAY"))),
        by_month_day=tuple(_integer(item, -31, 31, "BYMONTHDAY") for item in _items(parts.get("BYMONTHDAY"))),
        by_month=tuple(_integer(item, 1, 12, "BYMONTH") for item in _items(parts.get("BYMONTH"))),
        week_start=_weekday(parts.get("WKST", "MO")),
    )


# ---- candidate days -------------------------------------------------------------------------------


def _period_start(seed: date, rule: Rule, period: int) -> date:
    """First day of the ``period``-th period (each INTERVAL frequency units long) counted from DTSTART."""
    step = period * rule.interval
    if rule.freq == "DAILY":
        return seed + timedelta(days=step)
    if rule.freq == "WEEKLY":
        week = seed - timedelta(days=(seed.weekday() - rule.week_start) % 7)
        return week + timedelta(weeks=step)
    if rule.freq == "MONTHLY":
        index = seed.year * 12 + seed.month - 1 + step
        return date(index // 12, index % 12 + 1, 1)
    return date(seed.year + step, 1, 1)


def _skip_to(seed: date, rule: Rule, target: date) -> int:
    """A period index at or shortly before ``target``. Only valid without COUNT, which counts from DTSTART."""
    if target <= seed:
        return 0
    if rule.freq == "DAILY":
        elapsed = (target - seed).days
    elif rule.freq == "WEEKLY":
        elapsed = (target - _period_start(seed, rule, 0)).days // 7
    elif rule.freq == "MONTHLY":
        elapsed = (target.year - seed.year) * 12 + target.month - seed.month
    else:
        elapsed = target.year - seed.year
    return max(0, elapsed // rule.interval - 1)


def _month_dates(year: int, month: int) -> list[date]:
    return [date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)]


def _match_weekdays(dates: Sequence[date], by_day: Sequence[tuple[int, int]]) -> list[date]:
    """Dates matching BYDAY in one month or year: every such weekday, or the n-th (from the end if < 0)."""
    chosen: set[date] = set()
    for ordinal, weekday in by_day:
        same = [day for day in dates if day.weekday() == weekday]
        if ordinal == 0:
            chosen.update(same)
        elif -len(same) <= ordinal <= len(same):
            chosen.add(same[ordinal - 1] if ordinal > 0 else same[ordinal])
    return sorted(chosen)


def _day_numbers(values: Sequence[int], length: int) -> set[int]:
    """BYMONTHDAY values as day numbers in a month of ``length`` days; ``-1`` is the last day."""
    return {value if value > 0 else length + value + 1 for value in values}


def _month_days(year: int, month: int, rule: Rule, seed: date) -> list[date]:
    dates = _month_dates(year, month)
    if rule.by_month_day:
        wanted = _day_numbers(rule.by_month_day, len(dates))
        days = [day for day in dates if day.day in wanted]
        if rule.by_day:
            allowed = set(_match_weekdays(dates, rule.by_day))
            days = [day for day in days if day in allowed]
        return days
    if rule.by_day:
        return _match_weekdays(dates, rule.by_day)
    return [day for day in dates if day.day == seed.day]


def _year_days(year: int, rule: Rule, seed: date) -> list[date]:
    if rule.by_day and not rule.by_month and not rule.by_month_day:
        every_day = [day for month in range(1, 13) for day in _month_dates(year, month)]
        return _match_weekdays(every_day, rule.by_day)
    if rule.by_month:
        months: Sequence[int] = sorted(rule.by_month)
    elif rule.by_month_day or rule.by_day:
        months = range(1, 13)
    else:
        months = [seed.month]
    return [day for month in months for day in _month_days(year, month, rule, seed)]


def _limited(day: date, rule: Rule) -> bool:
    """BY* parts that filter (rather than expand) at this frequency (RFC 5545 §3.3.10 table)."""
    if rule.by_month and rule.freq != "YEARLY" and day.month not in rule.by_month:
        return False
    if rule.freq == "DAILY":
        length = calendar.monthrange(day.year, day.month)[1]
        if rule.by_month_day and day.day not in _day_numbers(rule.by_month_day, length):
            return False
        if rule.by_day and day.weekday() not in {weekday for _, weekday in rule.by_day}:
            return False
    return True


def _period_days(first: date, rule: Rule, seed: date) -> list[date]:
    if rule.freq == "DAILY":
        days = [first]
    elif rule.freq == "WEEKLY":
        weekdays = {weekday for _, weekday in rule.by_day} or {seed.weekday()}
        days = sorted(first + timedelta(days=(weekday - rule.week_start) % 7) for weekday in weekdays)
    elif rule.freq == "MONTHLY":
        days = _month_days(first.year, first.month, rule, seed)
    else:
        days = _year_days(first.year, rule, seed)
    return [day for day in days if _limited(day, rule)]


# ---- expansion ------------------------------------------------------------------------------------


def occurrences(
    dtstart: datetime, rule: Rule, lo: datetime, hi: datetime, *, limit: int = MAX_INSTANCES
) -> tuple[list[datetime], bool]:
    """Instance starts with ``lo <= start < hi`` and whether ``limit`` (or the period guard) cut them short.

    DTSTART is always the first instance and counts toward COUNT, even when it does not match the
    rule (RFC 5545 §3.3.10).
    """
    zone = dtstart.tzinfo or UTC
    seed = dtstart.replace(tzinfo=None)
    low, high = lo.astimezone(UTC), hi.astimezone(UTC)
    until = None if rule.until is None else rule.until.astimezone(UTC)
    stop = hi.astimezone(zone).date() + timedelta(days=1)
    found = [dtstart] if low <= dtstart.astimezone(UTC) < high else []
    emitted = 1
    period = 0 if rule.count is not None else _skip_to(seed.date(), rule, lo.astimezone(zone).date())
    for _ in range(MAX_PERIODS):
        first = _period_start(seed.date(), rule, period)
        if first > stop:
            return found, False
        for day in _period_days(first, rule, seed.date()):
            wall = datetime.combine(day, seed.time())
            if wall <= seed:
                continue
            moment = wall.replace(tzinfo=zone)
            instant = moment.astimezone(UTC)
            if (until is not None and instant > until) or (rule.count is not None and emitted >= rule.count):
                return found, False
            emitted += 1
            if instant >= high:
                return found, False
            if instant >= low:
                found.append(moment)
                if len(found) >= limit:
                    return found, True
        period += 1
    return found, True


def _label(event: Event) -> str:
    return f"event {truncate(event.summary or event.uid, 60)!r}"


def _instance_starts(
    event: Event, lo: datetime, hi: datetime, limit: int, warnings: list[str]
) -> list[datetime]:
    first_only = (
        [event.start] if lo.astimezone(UTC) <= event.start.astimezone(UTC) < hi.astimezone(UTC) else []
    )
    if not event.rrule:
        return first_only
    try:
        starts, truncated = occurrences(event.start, parse_rule(event.rrule, event.zone), lo, hi, limit=limit)
    except RuleError as exc:
        warnings.append(f"{_label(event)}: {exc}; showing only the first instance")
        return first_only
    except (ValueError, OverflowError):
        warnings.append(
            f"{_label(event)}: the recurrence could not be expanded; showing only the first instance"
        )
        return first_only
    if truncated:
        warnings.append(f"{_label(event)}: stopped after {limit} instances")
    return starts


def _span(event: Event) -> timedelta:
    """How long before a window an instance may start and still overlap it."""
    length = event.end.astimezone(UTC) - event.start.astimezone(UTC)
    return length + timedelta(days=1) if event.all_day else length


def _instance_end(event: Event, begin: datetime) -> datetime:
    """All-day events keep their length in calendar days; timed events keep their exact length."""
    if event.all_day:
        days = (event.end.astimezone(event.zone).date() - event.start.date()).days
        return begin + timedelta(days=days)
    length = event.end.astimezone(UTC) - event.start.astimezone(UTC)
    return (begin.astimezone(UTC) + length).astimezone(event.zone)


def _excluded(event: Event, begin: datetime) -> bool:
    return begin.astimezone(UTC) in event.exdates or begin.astimezone(event.zone).date() in event.exdays


def _overlaps(begin: datetime, finish: datetime, lo: datetime, hi: datetime) -> bool:
    """Half-open overlap; a zero-length instance counts when it starts inside the window."""
    first, last = begin.astimezone(UTC), finish.astimezone(UTC)
    low, high = lo.astimezone(UTC), hi.astimezone(UTC)
    return first < high and (last > low or (first == last and first >= low))


def expand(
    events: Sequence[Event], start: datetime, end: datetime, *, limit: int = MAX_INSTANCES
) -> tuple[list[Occurrence], list[str]]:
    """Instances overlapping ``[start, end)``, sorted by start, plus warnings about skipped rules.

    Events sharing a UID form one series: the event without RECURRENCE-ID is the master; each override
    replaces the master instance whose original start equals its RECURRENCE-ID. Cancelled overrides
    remove their instance.
    """
    warnings: list[str] = []
    masters: list[Event] = []
    overrides: dict[str, dict[datetime, Event]] = {}
    for event in events:
        if event.recurrence_id is None:
            masters.append(event)
        else:
            overrides.setdefault(event.uid, {})[event.recurrence_id.astimezone(UTC)] = event
    found: list[Occurrence] = []
    for master in masters:
        replaced = overrides.get(master.uid, {})
        for begin in _instance_starts(master, start - _span(master), end, limit, warnings):
            if begin.astimezone(UTC) in replaced or _excluded(master, begin):
                continue
            finish = _instance_end(master, begin)
            if _overlaps(begin, finish, start, end):
                found.append(Occurrence(master, begin, finish, bool(master.rrule)))
    found.extend(
        Occurrence(override, override.start, override.end, True)
        for series in overrides.values()
        for override in series.values()
        if override.status != "CANCELLED" and _overlaps(override.start, override.end, start, end)
    )
    found.sort(key=lambda item: (item.start.astimezone(UTC), item.event.summary, item.event.uid))
    return found, warnings
