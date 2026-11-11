"""Unit tests for tracking-number detection: a pattern alone is never enough."""

from __future__ import annotations

import pytest

from switchboard.connectors.shopping.tracking import find_tracking

UPS = "1ZEX4MPLE012345678"
USPS = "9400100000000000000000"
DHL_LINK = "https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id="


def found(text: str) -> list[tuple[str, str]]:
    return [(item.carrier, item.number) for item in find_tracking(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Tracking number: {UPS}", [("UPS", UPS)]),
        (f"Shipped with UPS ({UPS}).", [("UPS", UPS)]),
        ("UPS tracking: 1Z EX4 MPL E0 1234 5678", [("UPS", UPS)]),
        (f"USPS: {USPS}", [("USPS", USPS)]),
        ("Your tracking number is 9400 1000 0000 0000 0000 00.", [("USPS", USPS)]),
        ("Mailed by the Postal Service: 92055000000000000000", [("USPS", "92055000000000000000")]),
        ("FedEx tracking 123456789012", [("FedEx", "123456789012")]),
        ("Ships via FedEx Ground: 123456789012345", [("FedEx", "123456789012345")]),
        ("DHL Express waybill 1234567890 is on its way", [("DHL", "1234567890")]),
        ("Waybill 1234567890 (carrier: dhl)", [("DHL", "1234567890")]),
    ],
)
def test_finds_numbers_with_pattern_and_context(text: str, expected: list[tuple[str, str]]) -> None:
    assert found(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Call us at 5550100199 or 555-010-0199 any time.",
        "Tracking questions? Call 5550100199.",  # ten digits next to "tracking" still need "DHL"
        "Tracking number: 123456789012",  # twelve digits next to "tracking" still need "FedEx"
        "Your reference is 9400100000000000000000 for our records.",
        f"Promo code {UPS} applied at checkout.",
        "FedEx account 1234567890123 updated.",  # thirteen digits match no carrier
        "DHL hotline: 1-800-555-0199",
        "Order 4815162342 placed",
        "Card 1234 5678 9012 3456 was charged; see tracking below.",  # sixteen digits are not USPS
    ],
)
def test_ignores_digit_runs_and_phone_numbers_without_context(text: str) -> None:
    assert find_tracking(text) == []


def test_context_must_be_within_the_window() -> None:
    far = "Tracking details are in your account. " + "x" * 80 + " " + USPS
    assert find_tracking(far) == []


def test_excluded_order_id_is_never_reported_as_tracking() -> None:
    text = "FedEx order 123456789012 confirmed"
    assert found(text) == [("FedEx", "123456789012")]
    assert find_tracking(text, exclude=["123456789012"]) == []


def test_results_follow_text_order_without_duplicates() -> None:
    text = f"USPS tracking {USPS}. Also UPS tracking {UPS}. Again: tracking {USPS}."
    assert [item.number for item in find_tracking(text)] == [USPS, UPS]


def test_tracking_links_point_at_public_carrier_pages() -> None:
    text = f"UPS {UPS}; USPS {USPS}; FedEx 123456789012; DHL 1234567890"
    links = {item.carrier: item.url for item in find_tracking(text)}
    assert links == {
        "UPS": "https://www.ups.com/track?tracknum=" + UPS,
        "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels=" + USPS,
        "FedEx": "https://www.fedex.com/fedextrack/?trknbr=123456789012",
        "DHL": DHL_LINK + "1234567890",
    }
