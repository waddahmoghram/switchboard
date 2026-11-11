"""End-to-end: a full app with the shopping connector, FakeHttp for the web, and synthetic mail events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from switchboard.policy import Origin
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shopping"
ROBOTS = "https://shop.example.com/robots.txt"
PRODUCT = "https://shop.example.com/products/trail-pack-38l"
UPS = "1ZEX4MPLE012345678"
CONFIG: dict[str, Any] = {
    "switchboard": {"connectors": ["shopping"]},
    "connectors": {
        "shopping": {
            "allowed_domains": ["example.com"],
            "merchant_rules": [
                {
                    "name": "Example Outfitters",
                    "from_contains": "@orders.example.com",
                    "order_subject_regex": r"^Your Example Outfitters order",
                    "shipped_subject_regex": r"\bhas shipped\b",
                    "delivered_subject_regex": r"\bwas delivered\b",
                    "order_id_regex": r"Order (?:number|#)\s*:?\s*(EO-[0-9]{6})",
                    "total_regex": r"Order total:\s*([$]?[0-9,]+\.[0-9]{2})",
                    "currency": "USD",
                }
            ],
        }
    },
}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_price_watch_raises_a_price_drop_event(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nDisallow: /cart/\n")
    fake_http.add(
        "GET",
        PRODUCT,
        body=fixture_text("page_offers_list.html"),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

    added = app.call(
        "shopping_watch_add",
        {"url": PRODUCT, "target_price": "1300", "currency": "USD", "note": "for the trip"},
        Origin.ASSISTANT,
    )
    assert added.status == "ok", added.message

    app.ctx.scheduler.run_all()

    drops = [event for event in app.ctx.events.recent(50) if event.type == "shopping.price_drop"]
    assert [event.payload for event in drops] == [
        {
            "watch_id": 1,
            "url": PRODUCT,
            "title": "Trail Pack 38L",
            "price": "1299.00",
            "currency": "USD",
            "target": "1300.00",
            "previous": None,
        }
    ]
    assert [request.url for request in fake_http.requests] == [ROBOTS, PRODUCT]
    assert fake_http.requests[1].headers["accept"].startswith("text/html")

    (watch,) = app.call("shopping_watch_list", {}, Origin.ASSISTANT).data["watches"]
    assert (watch["last_price"], watch["last_availability"], watch["last_result"]) == (
        "1299.00",
        "InStock",
        "price",
    )

    # Checking again right away fetches nothing: the per-URL interval has not passed.
    again = app.call("shopping_check_prices", {}, Origin.ASSISTANT)
    assert (again.status, again.data["count"]) == ("ok", 0)
    assert len(fake_http.requests) == 2


def test_mail_events_become_orders_and_shipments(make_app: AppFactory) -> None:
    app = make_app(CONFIG)
    for name in ("mail_order_confirmation.json", "mail_shipped_ups.json", "mail_newsletter.json"):
        payload = json.loads(fixture_text(name))
        app.ctx.events.emit("mail", "mail.received", payload, dedupe_key=f"test:{name}")

    assert app.process_events() == 5  # three mails, one shopping.order, one shopping.shipment

    orders = app.call("shopping_orders", {}, Origin.ASSISTANT)
    assert orders.status == "ok", orders.message
    assert [
        (row["merchant"], row["order_id"], row["total"], row["currency"], row["status"])
        for row in orders.data["orders"]
    ] == [("Example Outfitters", "EO-104233", "1299.00", "USD", "shipped")]

    shipments = app.call("shopping_shipments", {"status": "shipped"}, Origin.ASSISTANT)
    (shipment,) = shipments.data["shipments"]
    assert (shipment["carrier"], shipment["tracking_number"], shipment["tracking_url"]) == (
        "UPS",
        UPS,
        "https://www.ups.com/track?tracknum=" + UPS,
    )
    assert shipment["timeline"]["ordered"] is not None
    assert shipment["timeline"]["shipped"] is not None
    assert shipment["timeline"]["delivered"] is None

    kinds = [event.type for event in app.ctx.events.recent(20)]
    assert kinds.count("shopping.order") == 1
    assert kinds.count("shopping.shipment") == 1
    assert all(event.payload.get("tracking_number") != "5550100199" for event in app.ctx.events.recent(20))
