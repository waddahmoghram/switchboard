"""Find carrier tracking numbers in untrusted email text and build public tracking links.

Detection is deliberately conservative. A candidate counts only when its pattern matches *and* the
text within ``CONTEXT_WINDOW`` characters before or after it mentions the word "tracking" or the
carrier. Digit-only carriers need their own name nearby, because bare 10- and 12-digit runs are
usually phone numbers, order numbers, or references:

* UPS: ``1Z`` plus 16 letters or digits (also in the printed ``1Z 999 AA1 01 2345 6784`` grouping);
  needs "tracking" or "UPS".
* USPS: 20 to 22 digits, contiguous or in groups of four; needs "tracking", "USPS", or
  "Postal Service".
* FedEx: exactly 12 or 15 digits; needs "FedEx".
* DHL: exactly 10 digits; needs "DHL".

Check digits are not validated. This project could not verify the carriers' algorithms from an
authoritative public source, so a pattern plus context is the whole test. Tracking-page URLs follow
patterns observed on 2026-11-11 and may change; they are links for a person to open, and Switchboard
never fetches them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

CONTEXT_WINDOW = 60
MAX_TRACKING_NUMBERS = 10

_BEFORE = r"(?<![0-9A-Za-z])"
_AFTER = r"(?![0-9A-Za-z])"


@dataclass(frozen=True)
class Tracking:
    carrier: str
    number: str
    url: str


@dataclass(frozen=True)
class _Carrier:
    name: str
    pattern: re.Pattern[str]
    context: re.Pattern[str]
    url_prefix: str


_CARRIERS: tuple[_Carrier, ...] = (
    _Carrier(
        "UPS",
        re.compile(_BEFORE + r"1Z ?[0-9A-Z]{3} ?[0-9A-Z]{3} ?[0-9A-Z]{2} ?[0-9A-Z]{4} ?[0-9A-Z]{4}" + _AFTER),
        re.compile(r"\bUPS\b|(?i:\btracking\b)"),
        "https://www.ups.com/track?tracknum=",
    ),
    _Carrier(
        "USPS",
        re.compile(_BEFORE + r"(?:[0-9]{20,22}|[0-9]{4}(?: [0-9]{4}){4}(?: [0-9]{1,2})?)" + _AFTER),
        re.compile(r"(?i:\btracking\b|\busps\b|\bpostal service\b)"),
        "https://tools.usps.com/go/TrackConfirmAction?tLabels=",
    ),
    _Carrier(
        "FedEx",
        re.compile(_BEFORE + r"(?:[0-9]{15}|[0-9]{12})" + _AFTER),
        re.compile(r"(?i:\bfedex\b)"),
        "https://www.fedex.com/fedextrack/?trknbr=",
    ),
    _Carrier(
        "DHL",
        re.compile(_BEFORE + r"[0-9]{10}" + _AFTER),
        re.compile(r"(?i:\bdhl\b)"),
        "https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id=",
    ),
)


def find_tracking(text: str, *, exclude: Iterable[str] = ()) -> list[Tracking]:
    """Tracking numbers in order of appearance, without duplicates or excluded values.

    Pass the order id in ``exclude`` so an all-digit order number is never mistaken for a parcel.
    """
    skip = {_compact(item) for item in exclude}
    hits: dict[str, tuple[int, Tracking]] = {}
    for carrier in _CARRIERS:
        for match in carrier.pattern.finditer(text):
            number = _compact(match.group(0))
            if number in skip or number in hits:
                continue
            if _has_context(text, match.start(), match.end(), carrier.context):
                url = carrier.url_prefix + quote(number, safe="")
                hits[number] = (match.start(), Tracking(carrier.name, number, url))
    ordered = sorted(hits.values(), key=lambda hit: hit[0])
    return [tracking for _, tracking in ordered[:MAX_TRACKING_NUMBERS]]


def _compact(value: str) -> str:
    return value.replace(" ", "").upper()


def _has_context(text: str, start: int, end: int, context: re.Pattern[str]) -> bool:
    before = text[max(0, start - CONTEXT_WINDOW) : start]
    after = text[end : end + CONTEXT_WINDOW]
    return context.search(before) is not None or context.search(after) is not None
