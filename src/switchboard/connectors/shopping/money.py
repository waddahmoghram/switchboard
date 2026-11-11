"""Prices as :class:`~decimal.Decimal`: read untrusted text without guessing locale conventions.

Floats are refused on purpose. JSON-LD is parsed with ``parse_float=Decimal``, so numbers never pass
through binary floating point, and text such as ``"1,299.00"`` is read digit by digit. A comma counts
only as a thousands separator in groups of three, and ``.`` is the only decimal mark, as schema.org
asks; ``"19,99"`` and ``"1.299,00"`` are rejected rather than guessed.
"""

from __future__ import annotations

import re
from decimal import Decimal

MAX_PRICE = Decimal("1000000000")
_SMALLEST_STEP = Decimal("0.0001")
_CENTS = Decimal("0.01")
_AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,12})(?:\.[0-9]{1,6})?"
_PRICE_TEXT = re.compile(r"(?:[A-Z]{3} ?)?[$€£₹]? ?(?P<amount>" + _AMOUNT + r")(?: ?[A-Z]{3})?")
_CODE = re.compile(r"[A-Z]{3}")
_CODE_WORD = re.compile(r"\b[A-Z]{3}\b")

# "$" is shared by many currencies, so it maps to nothing and the configured default applies.
CURRENCY_SYMBOLS: dict[str, str] = {"€": "EUR", "£": "GBP", "₹": "INR"}
# fmt: off
KNOWN_CURRENCIES = frozenset({
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "ILS",
    "INR", "JPY", "KRW", "MXN", "NOK", "NZD", "PLN", "SAR", "SEK", "SGD", "TRY", "USD", "ZAR",
})
# fmt: on


def parse_price(value: object) -> Decimal | None:
    """Return a positive amount with at most four significant decimals, or None.

    Accepts a Decimal, an int, or short text such as ``"49"``, ``"1,299.00"``, ``"$1,299.00"``, or
    ``"49.00 USD"``. Refuses floats, booleans, zero, negative or huge amounts, and ambiguous text.
    """
    amount = _to_decimal(value)
    if amount is None or not amount.is_finite() or amount <= 0 or amount > MAX_PRICE:
        return None
    if amount.quantize(_SMALLEST_STEP) != amount:
        return None
    return amount


def _to_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and len(value) <= 40:
        match = _PRICE_TEXT.fullmatch(value.strip())
        if match is not None:
            return Decimal(match.group("amount").replace(",", ""))
    return None


def price_text(amount: Decimal) -> str:
    """Canonical text without an exponent and with at least two decimals.

    ``Decimal("1E+3")`` becomes ``"1000.00"``, ``Decimal("19.9")`` becomes ``"19.90"``, and
    ``Decimal("0.125")`` stays ``"0.125"``. Used for storage, event payloads, and dedupe keys.
    """
    normalized = amount.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > -2:
        normalized = normalized.quantize(_CENTS)
    return format(normalized, "f")


def normalize_currency(value: object) -> str | None:
    """A currency code shaped like ISO 4217 (three ASCII letters, upper-cased), or None."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _CODE.fullmatch(code) else None


def currency_hint(text: str) -> str | None:
    """A currency named in ``text`` by a common ISO 4217 code or an unambiguous symbol."""
    for match in _CODE_WORD.finditer(text):
        if match.group(0) in KNOWN_CURRENCIES:
            return match.group(0)
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None
