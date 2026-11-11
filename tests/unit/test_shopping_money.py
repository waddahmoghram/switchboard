"""Unit tests for Decimal price parsing, canonical price text, and currency hints."""

from __future__ import annotations

from decimal import Decimal

import pytest

from switchboard.connectors.shopping.money import currency_hint, normalize_currency, parse_price, price_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,299.00", Decimal("1299.00")),
        ("$1,299.00", Decimal("1299.00")),
        ("12,345,678.90", Decimal("12345678.90")),
        ("1,299", Decimal("1299")),
        ("49", Decimal("49")),
        (" 49.95 ", Decimal("49.95")),
        ("49.00 USD", Decimal("49.00")),
        ("EUR 17.25", Decimal("17.25")),
        ("€17.25", Decimal("17.25")),
        ("19.990000", Decimal("19.99")),
        (Decimal("42.5"), Decimal("42.5")),
        (1299, Decimal("1299")),
    ],
)
def test_parse_price_reads_plain_amounts(raw: object, expected: Decimal) -> None:
    parsed = parse_price(raw)
    assert isinstance(parsed, Decimal)
    assert parsed == expected


@pytest.mark.parametrize(
    "raw",
    [
        "19,99",  # a decimal comma is ambiguous, so it is refused rather than read as 1999
        "1.299,00",
        "1,29",
        "-5.00",
        "0",
        "0.00",
        "free",
        "",
        "$",
        "1e3",
        "12.3456789",
        "2000000000",
        49.99,  # floats never enter price arithmetic
        True,
        None,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0.00001"),
    ],
)
def test_parse_price_refuses_ambiguous_or_unsafe_values(raw: object) -> None:
    assert parse_price(raw) is None


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (Decimal("1299"), "1299.00"),
        (Decimal("1299.00"), "1299.00"),
        (Decimal("1300"), "1300.00"),
        (Decimal("1E+3"), "1000.00"),
        (Decimal("19.9"), "19.90"),
        (Decimal("19.990000"), "19.99"),
        (Decimal("0.125"), "0.125"),
    ],
)
def test_price_text_is_canonical(amount: Decimal, text: str) -> None:
    assert price_text(amount) == text


def test_equal_prices_share_one_canonical_text() -> None:
    assert price_text(parse_price("1,299.00") or Decimal(0)) == price_text(Decimal("1299"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("usd", "USD"), (" eur ", "EUR"), ("US", None), ("USDT", None), ("€", None), (840, None)],
)
def test_normalize_currency(raw: object, expected: str | None) -> None:
    assert normalize_currency(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Order total: $45.00 USD", "USD"),
        ("Grand total: €89.90", "EUR"),
        ("Total: £12.00", "GBP"),
        ("Order total: $45.00", None),  # "$" alone is ambiguous
        ("TAX 45.00", None),
    ],
)
def test_currency_hint(text: str, expected: str | None) -> None:
    assert currency_hint(text) == expected
