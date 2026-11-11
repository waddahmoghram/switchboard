"""Free-time search: subtract busy intervals from working hours, day by day, in one time zone.

Pure functions only; callers pass the busy intervals, the window, and the zone explicitly. Working
hours are wall-clock times, so ``09:00-17:00`` stays the same on both sides of a daylight-saving change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-(?:([01]\d|2[0-3]):([0-5]\d)|(24:00))$")

Interval = tuple[datetime, datetime]


def parse_working_hours(text: str) -> tuple[time, time | None]:
    """Parse ``HH:MM-HH:MM``; an end of ``24:00`` (returned as None) means midnight after the day."""
    match = _HOURS.match(text.strip())
    if match is None:
        raise ValueError("working_hours must look like 09:00-17:00")
    start = time(int(match[1]), int(match[2]))
    end = None if match[5] else time(int(match[3]), int(match[4]))
    if end is not None and end <= start:
        raise ValueError("working_hours must end after they start")
    return start, end


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort intervals by start (as UTC instants) and join the ones that overlap or touch."""
    pairs = [(start.astimezone(UTC), end.astimezone(UTC)) for start, end in intervals]
    ordered = sorted(pair for pair in pairs if pair[1] > pair[0])
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def gaps(low: datetime, high: datetime, busy: list[Interval]) -> list[Interval]:
    """The parts of ``[low, high)`` not covered by ``busy`` (merged and sorted, in UTC)."""
    found: list[Interval] = []
    cursor = low
    for start, end in busy:
        if end <= cursor:
            continue
        if start >= high:
            break
        if start > cursor:
            found.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < high:
        found.append((cursor, high))
    return found


def _working_window(day: date, zone: tzinfo, day_start: time, day_end: time | None) -> Interval:
    opens = datetime.combine(day, day_start, tzinfo=zone)
    if day_end is None:
        closes = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    else:
        closes = datetime.combine(day, day_end, tzinfo=zone)
    return opens.astimezone(UTC), closes.astimezone(UTC)


def free_slots(
    busy: Iterable[Interval],
    start: datetime,
    end: datetime,
    *,
    zone: tzinfo,
    day_start: time,
    day_end: time | None,
    weekdays: set[int],
    minimum: timedelta,
    limit: int = 100,
) -> list[Interval]:
    """Free windows of at least ``minimum`` inside working hours on ``weekdays`` (0 is Monday).

    Results are UTC intervals in order, at most ``limit`` of them.
    """
    blocked = merge(busy)
    window_low, window_high = start.astimezone(UTC), end.astimezone(UTC)
    found: list[Interval] = []
    day = start.astimezone(zone).date()
    while day <= end.astimezone(zone).date() and len(found) < limit:
        if day.weekday() in weekdays:
            opens, closes = _working_window(day, zone, day_start, day_end)
            low, high = max(opens, window_low), min(closes, window_high)
            if low < high:
                found += [gap for gap in gaps(low, high, blocked) if gap[1] - gap[0] >= minimum]
        day += timedelta(days=1)
    return found[:limit]
