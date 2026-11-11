"""Merchant names, categories, and name similarity.

Card networks and payment processors decorate merchant names with prefixes, store numbers, and
reference codes: ``SQ *BLUE BOTTLE #0123``, ``AMAZON.COM*AB12CD3``, ``POS DEBIT 1105 EXAMPLE MARKET``.
A normalized merchant is upper case with those decorations removed, so the same shop compares equal
across months and across statement files and alert emails.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

from switchboard.connectors.finance.settings import UNCATEGORIZED, CategoryRule

_MAX_MERCHANT = 64
# Processor prefixes that end in "*" (Square, Toast, Shopify, PayPal) and card-network words.
_PREFIX = re.compile(r"^(?:(?:SQ|TST|SP|PP|PAYPAL)\s?\*\s*|(?:POS|DEBIT|CHECKCARD|PURCHASE|ACH)\s+)+")
# Reference codes and store numbers: "*AB12CD3", "#0123".
_REFERENCE = re.compile(r"[*#]\S*")


def normalize_merchant(text: str) -> str:
    """Upper-case merchant name without processor prefixes, reference codes, or numbers.

    Words with three or more digits (store numbers, dates, phone numbers) are dropped. When nothing is
    left, the collapsed original is used, so the result is empty only for empty input.
    """
    collapsed = " ".join(text.upper().split())
    stripped = _REFERENCE.sub(" ", _PREFIX.sub("", collapsed))
    words = [word for word in stripped.split() if sum(char.isdigit() for char in word) < 3]
    merchant = " ".join(words).strip(" -.,/")
    return (merchant or collapsed)[:_MAX_MERCHANT]


def categorize(rules: Sequence[CategoryRule], *texts: str) -> str:
    """The first category whose pattern matches any of ``texts``; rules keep configuration order."""
    for rule in rules:
        if any(pattern.search(text) for pattern in rule.patterns for text in texts if text):
            return rule.name
    return UNCATEGORIZED


def similarity(left: str, right: str) -> float:
    """How alike two normalized merchant names are, from 0.0 to 1.0."""
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()
