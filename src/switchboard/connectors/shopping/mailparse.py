"""Turn ``mail.received`` payloads into order and shipment notices.

Pure functions with no I/O. Mail is untrusted input, so only small, validated fields leave this module:
a merchant name from your rules (or the sender's domain), an order id with a restricted character set,
a Decimal total, a currency code, and tracking numbers. Subjects and bodies are never stored.

Generic heuristics are conservative: the subject must read like an order or shipping notice *and* the
message must carry an order id or a tracking number, so newsletters that say "on its way" are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from email.utils import parseaddr
from typing import Any

from switchboard.connectors.shopping.money import currency_hint, parse_price
from switchboard.connectors.shopping.settings import MerchantRule, Settings, is_domain
from switchboard.connectors.shopping.tracking import Tracking, find_tracking

STATUSES = ("ordered", "shipped", "out_for_delivery", "delivered")
MAX_HEADER = 500
MAX_TEXT = 20_000

_GENERIC_STATUS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("delivered", re.compile(r"\b(?:was|been|is) delivered\b|^delivered\b", re.IGNORECASE)),
    ("out_for_delivery", re.compile(r"\bout for delivery\b", re.IGNORECASE)),
    (
        "shipped",
        re.compile(
            r"\b(?:has|have) shipped\b|^shipped\b|\bshipping confirmation\b|\bis on (?:its|the) way\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ordered",
        re.compile(
            r"\border confirmation\b|\border (?:is )?confirmed\b|\bthanks? (?:you )?for your order\b"
            r"|\bwe(?:'ve| have) received your order\b",
            re.IGNORECASE,
        ),
    ),
)
_ORDER_ID = re.compile(
    r"\border\s*(?:number|no\.?|#|id)\s*[:#]?\s*"
    r"(?P<id>(?=[A-Z0-9-]*[0-9])[A-Z0-9][A-Z0-9-]{3,39})(?![A-Z0-9-])",
    re.IGNORECASE,
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,63}")
_TOTAL_LABELS = ("order total", "grand total", "total charged", "amount charged", "total")
_TOTAL_PATTERNS = tuple(
    re.compile(
        r"(?i:\b" + label.replace(" ", r"\s+") + r"\b)\s*:?\s*(?:[A-Z]{3}\s?)?[$€£₹]?\s?"
        r"(?P<amount>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]{1,9}(?:\.[0-9]{1,2})?)"
        r"(?![0-9]|[.,][0-9])"  # "19,99" or "1.299,00" is refused, never cut down to 19 or 1.29
        r"(?:\s?[A-Z]{3}\b)?"
    )
    for label in _TOTAL_LABELS
)


@dataclass(frozen=True)
class Notice:
    """What one email says about an order or a shipment."""

    status: str
    merchant: str
    order_id: str | None
    total: Decimal | None
    currency: str | None
    tracking: tuple[Tracking, ...]
    message_id: str | None


def parse_notice(payload: Mapping[str, Any], settings: Settings) -> Notice | None:
    """Classify one ``mail.received`` payload; None when it is not an order or shipping email."""
    sender = _text(payload.get("from"), MAX_HEADER)
    subject = _text(payload.get("subject"), MAX_HEADER)
    rule = next((item for item in settings.merchant_rules if item.matches_sender(sender)), None)
    status = _status(subject, rule)
    if status is None:
        return None
    content = subject + "\n" + _text(payload.get("text"), MAX_TEXT)
    order_id = _order_id(content, rule)
    tracking: tuple[Tracking, ...] = ()
    if status != "ordered":
        tracking = tuple(find_tracking(content, exclude=[order_id] if order_id else []))
    if order_id is None and not tracking:
        return None
    merchant = rule.name if rule is not None else _sender_domain(sender)
    if merchant is None:
        return None
    total: Decimal | None = None
    currency: str | None = None
    if status == "ordered":
        total, currency = _total(content, rule, settings.default_currency)
    return Notice(status, merchant, order_id, total, currency, tracking, _message_id(payload))


def _status(subject: str, rule: MerchantRule | None) -> str | None:
    """Merchant rules first (most advanced status wins), then the generic subject heuristics."""
    if rule is not None:
        candidates = (
            ("delivered", rule.delivered_subject),
            ("shipped", rule.shipped_subject),
            ("ordered", rule.order_subject),
        )
        for status, pattern in candidates:
            if pattern is not None and pattern.search(subject):
                return status
    for status, generic in _GENERIC_STATUS:
        if generic.search(subject):
            return status
    return None


def _order_id(content: str, rule: MerchantRule | None) -> str | None:
    if rule is not None and rule.order_id is not None:
        match = rule.order_id.search(content)
        if match is not None:
            candidate = _first_group(match).strip()
            if _SAFE_ID.fullmatch(candidate):
                return candidate
    generic = _ORDER_ID.search(content)
    return generic.group("id") if generic is not None else None


def _total(
    content: str, rule: MerchantRule | None, default_currency: str | None
) -> tuple[Decimal | None, str | None]:
    """The order total and its currency: rule currency, then a code or symbol by the total, then default."""
    patterns = list(_TOTAL_PATTERNS)
    if rule is not None and rule.total is not None:
        patterns.insert(0, rule.total)
    for pattern in patterns:
        match = pattern.search(content)
        amount = parse_price(_first_group(match)) if match is not None else None
        if match is not None and amount is not None:
            rule_currency = rule.currency if rule is not None else None
            return amount, rule_currency or currency_hint(match.group(0)) or default_currency
    return None, None


def _first_group(match: re.Match[str]) -> str:
    """Group 1 when the pattern has one and it took part in the match, else the whole match."""
    if match.re.groups >= 1 and match.group(1) is not None:
        return str(match.group(1))
    return match.group(0)


def _sender_domain(sender: str) -> str | None:
    address = parseaddr(sender)[1]
    domain = address.rpartition("@")[2].strip().lower().rstrip(".")
    return domain if "@" in address and is_domain(domain) else None


def _message_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("message_id")
    if not isinstance(value, str):
        return None
    return value.strip()[:300] or None


def _text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return ", ".join(item for item in value if isinstance(item, str))[:limit]
    return ""
