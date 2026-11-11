"""Unit tests for recording orders and shipment timelines from parsed mail."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.mailparse import Notice, parse_notice
from switchboard.connectors.shopping.orders import apply_notice
from switchboard.connectors.shopping.settings import Settings, parse_settings
from tests.conftest import AppFactory

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shopping"
T0 = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
UPS = "1ZEX4MPLE012345678"
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
CONFIG = {"switchboard": {"connectors": ["shopping"]}}


@pytest.fixture
def settings() -> Settings:
    return parse_settings({"merchant_rules": [RULE]})[0]


def notice(name: str, settings: Settings) -> Notice:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    parsed = parse_notice(payload, settings)
    assert parsed is not None, name
    return parsed


def record(app: App, parsed: Notice, hours: int) -> None:
    apply_notice(app.ctx.db, app.ctx.events, parsed, T0 + timedelta(hours=hours))


def events_of(app: App, kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in reversed(app.ctx.events.recent(100)) if event.type == kind]


def stamp(hours: int) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def test_order_then_shipping_then_delivery_builds_one_timeline(
    make_app: AppFactory, settings: Settings
) -> None:
    app = make_app(CONFIG)
    record(app, notice("mail_order_confirmation.json", settings), 0)
    record(app, notice("mail_shipped_ups.json", settings), 18)
    record(app, notice("mail_delivered.json", settings), 47)

    orders = store.list_orders(app.ctx.db, status=None, merchant="example outfitters", limit=10)
    assert [(row["order_id"], row["total"], row["currency"], row["status"]) for row in orders] == [
        ("EO-104233", "1299.00", "USD", "delivered")
    ]
    (shipment,) = store.list_shipments(app.ctx.db, status=None, limit=10)
    assert shipment["carrier"] == "UPS"
    assert shipment["tracking_number"] == UPS
    assert shipment["tracking_url"] == "https://www.ups.com/track?tracknum=" + UPS
    assert shipment["status"] == "delivered"
    assert shipment["timeline"] == {
        "ordered": stamp(0),
        "shipped": stamp(18),
        "out_for_delivery": None,
        "delivered": stamp(47),
    }
    assert events_of(app, "shopping.order") == [
        {
            "merchant": "Example Outfitters",
            "order_id": "EO-104233",
            "total": "1299.00",
            "currency": "USD",
            "source_message_id": "<order-104233@orders.example.com>",
        }
    ]
    assert [(event["status"], event["tracking_number"]) for event in events_of(app, "shopping.shipment")] == [
        ("shipped", UPS),
        ("delivered", UPS),
    ]


def test_repeated_email_changes_nothing(make_app: AppFactory, settings: Settings) -> None:
    app = make_app(CONFIG)
    shipped = notice("mail_shipped_ups.json", settings)
    record(app, shipped, 0)
    record(app, shipped, 1)
    assert len(store.list_shipments(app.ctx.db, status=None, limit=10)) == 1
    assert len(events_of(app, "shopping.shipment")) == 1


def test_late_shipping_email_never_moves_a_delivered_parcel_back(
    make_app: AppFactory, settings: Settings
) -> None:
    app = make_app(CONFIG)
    record(app, notice("mail_delivered.json", settings), 0)
    record(app, notice("mail_shipped_ups.json", settings), 1)

    (shipment,) = store.list_shipments(app.ctx.db, status=None, limit=10)
    assert shipment["status"] == "delivered"
    assert shipment["tracking_number"] == UPS  # the placeholder row took the tracking number
    assert shipment["timeline"]["shipped"] == stamp(1)
    assert [event["status"] for event in events_of(app, "shopping.shipment")] == ["delivered"]
    (order,) = store.list_orders(app.ctx.db, status=None, merchant=None, limit=10)
    assert (order["status"], order["total"]) == ("delivered", None)


def test_status_filter(make_app: AppFactory, settings: Settings) -> None:
    app = make_app(CONFIG)
    record(app, notice("mail_order_confirmation.json", settings), 0)
    record(app, notice("mail_generic_shipped_usps.json", settings), 1)

    shipped = store.list_shipments(app.ctx.db, status="shipped", limit=10)
    assert [(row["merchant"], row["carrier"]) for row in shipped] == [("gear.example.net", "USPS")]
    waiting = store.list_shipments(app.ctx.db, status="ordered", limit=10)
    assert [(row["merchant"], row["tracking_number"]) for row in waiting] == [("Example Outfitters", None)]
    assert store.list_orders(app.ctx.db, status="delivered", merchant=None, limit=10) == []
