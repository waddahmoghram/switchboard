"""Unit tests for turning synthetic mail into order and shipment notices."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors.shopping.mailparse import parse_notice
from switchboard.connectors.shopping.settings import Settings, parse_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shopping"
RULE = {
    "name": "Example Outfitters",
    "from_contains": "@orders.example.com",
    "order_subject_regex": r"^Your Example Outfitters order",
    "shipped_subject_regex": r"\bhas shipped\b",
    "delivered_subject_regex": r"\bwas delivered\b",
    "order_id_regex": r"Order (?:number|#)\s*:?\s*(EO-[0-9]{6})",
    "total_regex": r"Order total:\s*([$]?[0-9,]+\.[0-9]{2})",
    "currency": "USD",
}
UPS = "1ZEX4MPLE012345678"


def load_mail(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return payload


@pytest.fixture
def settings() -> Settings:
    parsed, problems = parse_settings({"merchant_rules": [RULE]})
    assert problems == []
    return parsed


def test_order_confirmation_from_merchant_rule(settings: Settings) -> None:
    notice = parse_notice(load_mail("mail_order_confirmation.json"), settings)
    assert notice is not None
    assert notice.status == "ordered"
    assert notice.merchant == "Example Outfitters"
    assert notice.order_id == "EO-104233"
    assert notice.total == Decimal("1299.00")
    assert notice.currency == "USD"
    assert notice.tracking == ()
    assert notice.message_id == "<order-104233@orders.example.com>"


def test_shipping_notice_finds_ups_but_not_phone_numbers(settings: Settings) -> None:
    notice = parse_notice(load_mail("mail_shipped_ups.json"), settings)
    assert notice is not None
    assert notice.status == "shipped"
    assert notice.order_id == "EO-104233"
    assert [(item.carrier, item.number) for item in notice.tracking] == [("UPS", UPS)]
    assert notice.tracking[0].url == "https://www.ups.com/track?tracknum=" + UPS
    assert notice.total is None


def test_delivery_notice_without_tracking_number(settings: Settings) -> None:
    notice = parse_notice(load_mail("mail_delivered.json"), settings)
    assert notice is not None
    assert (notice.status, notice.order_id, notice.tracking) == ("delivered", "EO-104233", ())


def test_out_for_delivery_falls_back_to_generic_subject_words(settings: Settings) -> None:
    payload = {
        "from": "Example Outfitters <ship@orders.example.com>",
        "subject": "Out for delivery: your package",
        "text": "Order number: EO-104233 arrives today.",
    }
    notice = parse_notice(payload, settings)
    assert notice is not None
    assert (notice.status, notice.order_id) == ("out_for_delivery", "EO-104233")


def test_generic_order_uses_sender_domain_and_currency_symbol(settings: Settings) -> None:
    notice = parse_notice(load_mail("mail_generic_order_eur.json"), settings)
    assert notice is not None
    assert notice.status == "ordered"
    assert notice.merchant == "gear.example.net"
    assert notice.order_id == "EG-55821"
    assert notice.total == Decimal("89.90")
    assert notice.currency == "EUR"


def test_generic_shipping_notice_with_grouped_usps_number(settings: Settings) -> None:
    notice = parse_notice(load_mail("mail_generic_shipped_usps.json"), settings)
    assert notice is not None
    assert notice.status == "shipped"
    assert notice.order_id == "EG-55821"
    assert [(item.carrier, item.number) for item in notice.tracking] == [("USPS", "9400100000000000000000")]


def test_newsletter_that_sounds_like_shipping_is_ignored(settings: Settings) -> None:
    assert parse_notice(load_mail("mail_newsletter.json"), settings) is None


def test_order_subject_without_order_id_is_ignored(settings: Settings) -> None:
    payload = {"from": "Shop <news@shop.example.com>", "subject": "Order confirmation", "text": "Thanks!"}
    assert parse_notice(payload, settings) is None


def test_unrelated_mail_is_ignored(settings: Settings) -> None:
    payload = {"from": "Friend <friend@example.org>", "subject": "Lunch?", "text": "Order 12345 of pizza?"}
    assert parse_notice(payload, settings) is None


def test_dollar_total_uses_configured_default_currency() -> None:
    settings, _ = parse_settings({"default_currency": "CAD"})
    payload = {
        "from": "Example Gear Shop <no-reply@gear.example.net>",
        "subject": "Thank you for your order",
        "text": "Order number: EG-60001\nOrder total: $45.00\n",
    }
    notice = parse_notice(payload, settings)
    assert notice is not None
    assert (notice.total, notice.currency) == (Decimal("45.00"), "CAD")


def test_dollar_total_without_default_has_no_currency() -> None:
    payload = {
        "from": "Example Gear Shop <no-reply@gear.example.net>",
        "subject": "Order confirmation",
        "text": "Order ID: EG-60002\nItem total: $5.00\nOrder total: $45.00 total\n",
    }
    notice = parse_notice(payload, Settings())
    assert notice is not None
    assert (notice.order_id, notice.total, notice.currency) == ("EG-60002", Decimal("45.00"), None)


@pytest.mark.parametrize("total", ["19,99 \u20ac", "\u20ac1.299,00", "1.299,00 EUR", "$19.999"])
def test_totals_that_would_be_cut_short_are_refused(total: str) -> None:
    payload = {
        "from": "Example Gear Shop <no-reply@gear.example.net>",
        "subject": "Order confirmation",
        "text": f"Order ID: EG-60003\nGrand total: {total}\n",
    }
    notice = parse_notice(payload, Settings())
    assert notice is not None
    assert (notice.order_id, notice.total) == ("EG-60003", None)
