"""Build iMIP invitations (RFC 6047) carrying an iTIP REQUEST (RFC 5546 §3.2.2).

The message is ``multipart/alternative`` with a readable ``text/plain`` part followed by a
``text/calendar; method=REQUEST`` part (RFC 6047 §2.4 and the low-fidelity example in §4.2). The
calendar object carries METHOD:REQUEST, ORGANIZER, one ATTENDEE per invitee with RSVP=TRUE, UID,
DTSTAMP, and SEQUENCE:0, which RFC 5546 requires for a new REQUEST.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, tzinfo
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from switchboard.connectors.calendar.ics import Event, serialize, zone_key


def describe_when(event: Event, zone: tzinfo) -> str:
    """A short human time range in ``zone``, such as ``2026-11-12 09:00-10:00 (Europe/Berlin)``."""
    start = event.start.astimezone(zone)
    end = event.end.astimezone(zone)
    if event.all_day:
        last = event.end.astimezone(event.zone).date().toordinal() - event.start.date().toordinal()
        days = "" if last <= 1 else f" ({last} days)"
        return f"{event.start.date().isoformat()}, all day{days}"
    same_day = start.date() == end.date()
    finish = end.strftime("%H:%M") if same_day else end.strftime("%Y-%m-%d %H:%M")
    return f"{start.strftime('%Y-%m-%d %H:%M')}-{finish} ({zone_key(zone)})"


def _plain_text(event: Event, zone: tzinfo) -> str:
    lines = [event.summary, "", f"When: {describe_when(event, zone)}"]
    if event.location:
        lines.append(f"Where: {event.location}")
    if event.attendees:
        lines.append(f"Invited: {', '.join(event.attendees)}")
    if event.description:
        lines += ["", event.description]
    lines += ["", "The attached calendar invitation (text/calendar, METHOD:REQUEST) adds this event."]
    return "\n".join(lines) + "\n"


def _domain(address: str) -> str:
    _, _, domain = address.rpartition("@")
    return domain or "switchboard.invalid"


def build_request(
    event: Event,
    *,
    organizer: str,
    recipients: Sequence[str],
    stamp: datetime,
    zone: tzinfo,
) -> EmailMessage:
    """An invitation for ``event`` from ``organizer`` to ``recipients``.

    ``event.organizer`` and ``event.attendees`` should already be set; ``recipients`` only fills the
    To header (a self-addressed draft goes to the organizer).
    """
    message = EmailMessage()
    message["Subject"] = f"Invitation: {' '.join(event.summary.split())} ({describe_when(event, zone)})"
    message["From"] = organizer
    message["To"] = ", ".join(recipients)
    message["Date"] = format_datetime(stamp)
    # An explicit domain avoids make_msgid's default lookup of this machine's host name.
    message["Message-ID"] = make_msgid(domain=_domain(organizer))
    message.set_content(_plain_text(event, zone))
    message.add_alternative(
        serialize(event, stamp=stamp, method="REQUEST"), subtype="calendar", params={"method": "REQUEST"}
    )
    return message
