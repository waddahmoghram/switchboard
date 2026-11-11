"""Parse and render money. Amounts are integer cents everywhere; floats appear only in JSON output."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
# Whitespace (including no-break spaces) and the dollar, euro, pound, and yen signs.
_DECORATION = re.compile(r"[\s$\u20ac\u00a3\u00a5]")
# A quadrillion in any currency: only corrupt data is larger, and smaller cents fit SQLite's 64-bit integers.
_LIMIT = Decimal(10) ** 15


def to_cents(value: Decimal) -> int:
    """Round a decimal amount to whole cents; halves round away from zero.

    Raises ValueError for amounts of a quadrillion or more, which would otherwise surface as
    ``decimal.InvalidOperation`` or a SQLite ``OverflowError`` that no caller expects.
    """
    if not value.is_finite() or abs(value) >= _LIMIT:
        raise ValueError("the amount is out of range")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def decimal_from(text: str) -> Decimal:
    """Parse a plain signed decimal such as ``-12.5``, ``+3``, or ``.5``; raise ValueError otherwise.

    The pattern excludes ``NaN``, ``Infinity``, and exponents, which :class:`~decimal.Decimal` would
    otherwise accept.
    """
    if not _PLAIN_NUMBER.fullmatch(text):
        raise ValueError("not a plain decimal number")
    return Decimal(text)


def parse_ofx_amount(text: str) -> int:
    """Read an OFX amount: an optional sign, ``.`` or ``,`` as the decimal mark, no thousands separators."""
    return to_cents(decimal_from(text.strip().replace(",", ".")))


def parse_money(text: str, decimal_mark: str = ".") -> int:
    """Read a lenient amount from a CSV export or an alert email; raise ValueError when it is not one.

    Accepts currency symbols, thousands separators, a leading or trailing minus, a Unicode minus sign,
    and accounting parentheses: ``$1,234.56``, ``-$5.00``, ``12.34-``, ``(12.34)``. With
    ``decimal_mark=","`` the roles of ``.`` and ``,`` swap, as in ``1.234,56``.
    """
    cleaned = _DECORATION.sub("", text.replace("\u2212", "-"))
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1]
    if cleaned.endswith("-"):
        negative, cleaned = not negative, cleaned[:-1]
    thousands = "," if decimal_mark == "." else "."
    cleaned = cleaned.replace(thousands, "")
    if decimal_mark != ".":
        cleaned = cleaned.replace(decimal_mark, ".")
    cents = to_cents(decimal_from(cleaned))
    return -cents if negative else cents


def cents_from_number(value: float) -> int:
    """Convert a number from tool arguments or the configuration to cents.

    Going through ``str`` keeps the decimal digits a person typed (``12.3`` stays 12.30, not
    12.2999...). Raises ValueError for NaN and infinities.
    """
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("amount must be a finite number")
    return to_cents(number)


def cents_to_number(cents: int) -> float:
    """Render cents as a JSON number rounded to cents: 1250 -> 12.5. Never used for arithmetic."""
    return float(Decimal(cents).scaleb(-2))


def format_cents(cents: int) -> str:
    """Render cents for people, with thousands separators: -123450 -> ``-1,234.50``."""
    return f"{Decimal(cents).scaleb(-2):,.2f}"
