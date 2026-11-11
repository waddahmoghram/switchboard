"""Unit tests for price extraction from JSON-LD and product meta tags."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from switchboard.connectors.shopping.pages import clean_text, extract_price

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shopping"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_product_with_offers_list_uses_the_first_priced_offer() -> None:
    found = extract_price(page("page_offers_list.html"))
    assert found is not None
    assert found.price == Decimal("1299.00")
    assert (found.currency, found.availability, found.source) == ("USD", "InStock", "json-ld")
    assert found.title == "Trail Pack 38L"  # the product name, not the related item or og:title


def test_product_inside_graph_with_numeric_price() -> None:
    found = extract_price(page("page_graph.html"))
    assert found is not None
    assert isinstance(found.price, Decimal)
    assert found.price == Decimal("42.50")
    assert (found.currency, found.availability, found.title) == ("USD", "OutOfStock", "Camp Stove")


def test_aggregate_offer_uses_low_price() -> None:
    found = extract_price(page("page_aggregate_offer.html"))
    assert found is not None
    assert (found.price, found.currency, found.title) == (Decimal("24.99"), "USD", "Headlamp 400")


def test_malformed_script_is_skipped_and_a_bare_offer_is_used() -> None:
    found = extract_price(page("page_malformed_then_offer.html"))
    assert found is not None
    assert (found.price, found.currency) == (Decimal("17.25"), "EUR")
    assert found.title == "Water Bottle | Example Outfitters"


def test_product_meta_tags_are_the_fallback() -> None:
    found = extract_price(page("page_opengraph.html"))
    assert found is not None
    assert found.price == Decimal("89.00")  # not og:price:standard_amount, the pre-sale price
    assert (found.currency, found.availability, found.title, found.source) == (
        "USD",
        "in stock",
        "Fleece Jacket",
        "meta",
    )


def test_legacy_og_price_tags_are_understood() -> None:
    html = (
        '<html><head><meta property="og:price:amount" content="15.50">'
        '<meta property="og:price:currency" content="gbp"><title>Socks</title></head></html>'
    )
    found = extract_price(html)
    assert found is not None
    assert (found.price, found.currency, found.title) == (Decimal("15.50"), "GBP", "Socks")


def test_visible_prices_are_never_scraped() -> None:
    assert extract_price(page("page_no_price.html")) is None


def test_numbers_in_json_ld_never_become_floats() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Tent", "offers": {"price": 1299.99, "priceCurrency": "USD"}}'
        "</script>"
    )
    found = extract_price(html)
    assert found is not None
    assert isinstance(found.price, Decimal)
    assert str(found.price) == "1299.99"


def test_nan_price_is_ignored_and_meta_tags_still_answer() -> None:
    html = (
        '<script type="application/ld+json">{"@type": "Product", "offers": {"price": NaN}}</script>'
        '<meta name="product:price:amount" content="1,050.00">'
    )
    found = extract_price(html)
    assert found is not None
    assert (found.price, found.source) == (Decimal("1050.00"), "meta")


def test_related_product_price_is_not_mistaken_for_the_main_product() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Kayak", "offers": {"@type": "Offer", "availability": "SoldOut"},'
        ' "isSimilarTo": {"@type": "Product", "offers": {"price": "9.99", "priceCurrency": "USD"}}}'
        "</script>"
    )
    assert extract_price(html) is None


def test_price_specification_and_type_iris_are_understood() -> None:
    html = (
        '<script type="application/ld+json; charset=utf-8">'
        '[{"@type": "http://schema.org/Product", "name": "Stove Fuel", "offers": {"@type": "schema:Offer",'
        ' "priceSpecification": {"price": "7.95", "priceCurrency": "CAD"}}}]'
        "</script>"
    )
    found = extract_price(html)
    assert found is not None
    assert (found.price, found.currency, found.title) == (Decimal("7.95"), "CAD", "Stove Fuel")


def test_clean_text_removes_control_and_bidi_characters_and_caps_length() -> None:
    assert clean_text("Trail\u202e Pack\x00\n 38L", 120) == "Trail Pack 38L"
    assert clean_text("x" * 200, 10) == "xxxxxxxxx…"
    assert clean_text("   ", 10) is None
    assert clean_text(42, 10) is None
