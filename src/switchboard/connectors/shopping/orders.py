"""Record orders and shipments found in mail and announce them as events.

``shopping.order`` is emitted once per merchant and order id. ``shopping.shipment`` is emitted when a
shipment appears with a shipping status or moves forward (shipped, out for delivery, delivered), once
per shipment and status.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.mailparse import Notice, parse_notice
from switchboard.connectors.shopping.money import price_text
from switchboard.connectors.shopping.settings import NAME, Settings
from switchboard.db import Database, transaction
from switchboard.events import Event, EventBus
from switchboard.util import iso, now


def handle_mail(db: Database, events: EventBus, settings: Settings, event: Event) -> None:
    """Subscriber for ``mail.received``; ignores everything that is not an order or shipping notice."""
    notice = parse_notice(event.payload, settings)
    if notice is not None:
        apply_notice(db, events, notice, now())


def apply_notice(
    db: Database, events: EventBus, notice: Notice, moment: datetime
) -> list[store.ShipmentChange]:
    """Store one notice in a single transaction, then emit the resulting events."""
    stamp = iso(moment)
    with db.session() as conn, transaction(conn):
        store.upsert_order(conn, notice, stamp)
        changes = store.apply_shipment(conn, notice, stamp)
    if notice.status == "ordered" and notice.order_id is not None:
        dedupe_key = f"shopping:order:{notice.merchant.lower()}:{notice.order_id}"
        events.emit(NAME, "shopping.order", order_payload(notice), dedupe_key=dedupe_key)
    for change in changes:
        if change.status != "ordered":
            dedupe_key = f"shopping:shipment:{change.key}:{change.status}"
            events.emit(NAME, "shopping.shipment", change.payload(), dedupe_key=dedupe_key)
    return changes


def order_payload(notice: Notice) -> dict[str, Any]:
    return {
        "merchant": notice.merchant,
        "order_id": notice.order_id,
        "total": price_text(notice.total) if notice.total is not None else None,
        "currency": notice.currency,
        "source_message_id": notice.message_id,
    }
