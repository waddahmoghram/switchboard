"""CalDAV (RFC 4791) operations on top of the shared WebDAV helper.

Reading uses a ``calendar-query`` REPORT (§7.8) with a VEVENT ``comp-filter`` and a UTC ``time-range``
(§9.9), so the server returns only resources with an instance in the window; recurring series come
back whole and are expanded locally. Writing is a single ``PUT`` of a new resource with
``If-None-Match: *`` (§5.3.2), which can create an event but never overwrite one.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from switchboard.connectors._dav import (
    CALDAV,
    DISPLAYNAME,
    XMLNS,
    DavClient,
    DavError,
    Flavor,
    Resource,
    collection_url,
    discover,
    qname,
)
from switchboard.connectors.calendar.ics import format_utc

CALENDAR_DATA = qname(CALDAV, "calendar-data")
SUPPORTED_COMPONENTS = qname(CALDAV, "supported-calendar-component-set")
FLAVOR = Flavor(
    well_known="/.well-known/caldav",
    home_set=qname(CALDAV, "calendar-home-set"),
    collection_type=qname(CALDAV, "calendar"),
    noun="calendar",
    extra_props=(SUPPORTED_COMPONENTS,),
)
CONTENT_TYPE = "text/calendar; charset=utf-8"

_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query {xmlns}>
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""


@dataclass(frozen=True)
class CalendarCollection:
    url: str
    name: str


def _holds_events(resource: Resource) -> bool:
    """RFC 4791 §5.2.3: without the property, a calendar accepts every component type."""
    element = resource.props.get(SUPPORTED_COMPONENTS)
    if element is None:
        return True
    return any((child.get("name") or "").upper() == "VEVENT" for child in element)


def _name(resource: Resource) -> str:
    return resource.text(DISPLAYNAME) or urllib.parse.unquote(resource.href.rstrip("/").rsplit("/", 1)[-1])


def find_calendars(client: DavClient) -> list[CalendarCollection]:
    """Calendars that hold events, from the configured collection URL or by discovery."""
    calendars = [
        CalendarCollection(resource.href, _name(resource))
        for resource in discover(client, FLAVOR)
        if _holds_events(resource)
    ]
    if not calendars:
        raise DavError(f"{client.label}: no calendar that holds events was found")
    return calendars


def query_body(start: datetime, end: datetime) -> bytes:
    return _QUERY.format(xmlns=XMLNS, start=format_utc(start), end=format_utc(end)).encode()


def query_events(client: DavClient, url: str, start: datetime, end: datetime) -> list[str]:
    """The iCalendar text of every resource with an event instance in ``[start, end)``."""
    resources = client.report(collection_url(url), query_body(start, end), depth="1")
    return [text for resource in resources if (text := resource.text(CALENDAR_DATA))]


def put_event(client: DavClient, url: str, uid: str, ics_text: str) -> str:
    """Create ``<collection>/<uid>.ics``; returns the new resource's URL."""
    target = urllib.parse.urljoin(collection_url(url), urllib.parse.quote(uid, safe="") + ".ics")
    client.put(target, ics_text.encode("utf-8"), CONTENT_TYPE, create_only=True)
    return target
