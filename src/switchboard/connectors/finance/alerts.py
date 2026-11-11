"""Turn bank alert emails into pending transactions.

Many banks can email you within minutes of a card charge or a deposit. Each
``[[connectors.finance.alert_rules]]`` entry recognizes one kind of alert: the sender must match one of
its globs, the optional subject regex must match, and the body regex must find a named ``amount``
group. Optional groups ``merchant``, ``last4``, and ``date`` fill in the rest.

Alert emails are untrusted input. Anyone can send a message that looks like an alert, and ``From``
addresses can be forged. Alerts therefore only create *pending* ledger entries, which a later statement
import confirms (see :mod:`switchboard.connectors.finance.ledger`). Nothing here can move money.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from switchboard.connectors.finance.csvfile import strptime_date
from switchboard.connectors.finance.models import FormatError
from switchboard.connectors.finance.money import parse_money
from switchboard.connectors.finance.settings import AlertRule
from switchboard.util import canonical_json, glob_any, sha256_hex

# The mail connector caps message text at 20,000 characters; enforce the same bound here.
MAX_TEXT = 20_000
_MAX_MERCHANT_TEXT = 120


@dataclass(frozen=True)
class AlertMatch:
    """What one alert email says. ``amount_cents`` is negative for money out."""

    rule: str
    account: str
    last4: str
    posted: date
    amount_cents: int
    merchant: str
    currency: str
    message_key: str


def match_alert(
    rules: Sequence[AlertRule], payload: Mapping[str, Any], *, today: date, zone: tzinfo | None = None
) -> AlertMatch | None:
    """Apply the first rule that recognizes a ``mail.received`` payload, or return None.

    ``today`` is the fallback date when neither the body nor the ``date`` header gives one; ``zone``
    turns a header's moment into a calendar date (None: this computer's time zone).
    Raises FormatError when a rule matches but its ``amount`` group is not a number.
    """
    sender = sender_address(payload.get("from"))
    subject = _string(payload.get("subject"))
    body = _string(payload.get("text"))[:MAX_TEXT]
    for rule in rules:
        if not glob_any(sender, rule.senders):
            continue
        if rule.subject is not None and rule.subject.search(subject) is None:
            continue
        found = rule.body.search(body)
        if found is not None:
            return _alert(rule, found, payload, today, zone)
    return None


def sender_address(value: Any) -> str:
    """The bare, lower-case address from a ``from`` value: a string, a list, or a mapping."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, Mapping):
        value = value.get("address") or value.get("email") or ""
    return parseaddr(value)[1].lower() if isinstance(value, str) else ""


def message_key(payload: Mapping[str, Any]) -> str:
    """A stable dedupe key: a hash of the Message-ID, or of the visible fields when there is none."""
    message_id = payload.get("message_id")
    if isinstance(message_id, str) and message_id.strip():
        basis = "message-id:" + message_id.strip()
    else:
        fields = [payload.get(key) for key in ("from", "subject", "date", "text")]
        basis = "fields:" + canonical_json(fields)
    return sha256_hex(basis)[:40]


def _alert(
    rule: AlertRule, found: re.Match[str], payload: Mapping[str, Any], today: date, zone: tzinfo | None
) -> AlertMatch:
    groups = found.groupdict()
    try:
        cents = abs(parse_money(groups.get("amount") or ""))
    except ValueError as exc:
        raise FormatError(f"alert rule {rule.name!r}: the amount group is not a number") from exc
    digits = "".join(char for char in groups.get("last4") or "" if char.isdigit())
    return AlertMatch(
        rule=rule.name,
        account=rule.account,
        last4=digits[-4:],
        posted=_alert_date(groups.get("date"), rule.date_formats, payload.get("date"), today, zone),
        amount_cents=-cents if rule.direction == "out" else cents,
        merchant=" ".join((groups.get("merchant") or "").split())[:_MAX_MERCHANT_TEXT],
        currency=rule.currency,
        message_key=message_key(payload),
    )


def _alert_date(
    text: str | None, formats: Sequence[str], header: Any, today: date, zone: tzinfo | None
) -> date:
    """The date in the body, else the email's ``date`` header, else ``today``."""
    if text:
        for fmt in formats:
            parsed = strptime_date(text.strip(), fmt)
            if parsed is not None:
                return parsed
    return header_date(header, zone) or today


def header_date(value: Any, zone: tzinfo | None = None) -> date | None:
    """The calendar date of an email's ``date`` value in ``zone`` (None: this computer's time zone).

    Accepts RFC 5322 dates (``Thu, 05 Nov 2026 18:30:00 -0800``) and ISO 8601 moments; the mail
    connector sends UTC moments such as ``2026-11-06T02:30:00+00:00``, which is still November 5 in
    California. A value without a zone (``-0000``, a bare date) keeps its own date.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    moment = _moment(value.strip())
    if moment is None:
        with contextlib.suppress(ValueError):
            return date.fromisoformat(value.strip()[:10])
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(zone).date()


def _moment(text: str) -> datetime | None:
    with contextlib.suppress(TypeError, ValueError, IndexError):
        return parsedate_to_datetime(text)
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text)
    return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
