"""SQLite storage for orders, shipments, price watches, and price history.

Every statement is static and parameterized. Timestamps are UTC ISO 8601 strings from
:func:`switchboard.util.iso`, so they sort and compare as text. Prices are stored as canonical text
(see :func:`~switchboard.connectors.shopping.money.price_text`) and read back as Decimal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from switchboard.connectors.shopping.mailparse import STATUSES, Notice
from switchboard.connectors.shopping.money import price_text
from switchboard.connectors.shopping.pages import PriceFound
from switchboard.connectors.shopping.tracking import Tracking
from switchboard.db import Database, Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import iso

MAX_ACTIVE_WATCHES = 100

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE shopping_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant TEXT NOT NULL COLLATE NOCASE,
                order_id TEXT NOT NULL,
                total TEXT,
                currency TEXT,
                status TEXT NOT NULL
                    CHECK (status IN ('ordered', 'shipped', 'out_for_delivery', 'delivered')),
                source_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (merchant, order_id)
            )
            """,
            """
            CREATE TABLE shopping_shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shipment_key TEXT NOT NULL UNIQUE,
                merchant TEXT NOT NULL COLLATE NOCASE,
                order_id TEXT,
                carrier TEXT,
                tracking_number TEXT,
                tracking_url TEXT,
                status TEXT NOT NULL
                    CHECK (status IN ('ordered', 'shipped', 'out_for_delivery', 'delivered')),
                ordered_at TEXT,
                shipped_at TEXT,
                out_for_delivery_at TEXT,
                delivered_at TEXT,
                source_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX shopping_shipments_order ON shopping_shipments(merchant, order_id)",
            "CREATE INDEX shopping_shipments_status ON shopping_shipments(status, updated_at)",
            """
            CREATE TABLE shopping_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT,
                target_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                note TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                next_allowed_at TEXT,
                last_checked_at TEXT,
                last_result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE shopping_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL REFERENCES shopping_watches(id) ON DELETE CASCADE,
                observed_at TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT,
                availability TEXT,
                source TEXT NOT NULL
            )
            """,
            "CREATE INDEX shopping_prices_watch ON shopping_prices(watch_id, id)",
        ),
    ),
]

# One static statement per status keeps the SQL free of string building. Columns: status, the
# status's first-seen time (kept once set), source message, updated_at, and the row id.
_ADVANCE: dict[str, str] = {
    "ordered": "UPDATE shopping_shipments SET status = ?, ordered_at = COALESCE(ordered_at, ?), "
    "source_message_id = COALESCE(?, source_message_id), updated_at = ? WHERE id = ?",
    "shipped": "UPDATE shopping_shipments SET status = ?, shipped_at = COALESCE(shipped_at, ?), "
    "source_message_id = COALESCE(?, source_message_id), updated_at = ? WHERE id = ?",
    "out_for_delivery": "UPDATE shopping_shipments SET status = ?, "
    "out_for_delivery_at = COALESCE(out_for_delivery_at, ?), "
    "source_message_id = COALESCE(?, source_message_id), updated_at = ? WHERE id = ?",
    "delivered": "UPDATE shopping_shipments SET status = ?, delivered_at = COALESCE(delivered_at, ?), "
    "source_message_id = COALESCE(?, source_message_id), updated_at = ? WHERE id = ?",
}


def later_status(current: str, incoming: str) -> str:
    """The more advanced of two statuses, so a late or repeated email never moves a parcel backwards."""
    return incoming if _rank(incoming) > _rank(current) else current


def _rank(status: str) -> int:
    return STATUSES.index(status) if status in STATUSES else -1


# ---- Orders and shipments ------------------------------------------------------------------


@dataclass(frozen=True)
class ShipmentChange:
    """A shipment that was created or moved forward, ready to announce as ``shopping.shipment``."""

    key: str
    merchant: str
    order_id: str | None
    carrier: str | None
    tracking_number: str | None
    tracking_url: str | None
    status: str

    def payload(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "order_id": self.order_id,
            "carrier": self.carrier,
            "tracking_number": self.tracking_number,
            "tracking_url": self.tracking_url,
            "status": self.status,
        }


def upsert_order(conn: sqlite3.Connection, notice: Notice, stamp: str) -> None:
    """Create the order, or fill in a missing total and move its status forward."""
    if notice.order_id is None:
        return
    total = price_text(notice.total) if notice.total is not None else None
    row = conn.execute(
        "SELECT id, status FROM shopping_orders WHERE merchant = ? AND order_id = ?",
        (notice.merchant, notice.order_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO shopping_orders (merchant, order_id, total, currency, status, source_message_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notice.merchant,
                notice.order_id,
                total,
                notice.currency,
                notice.status,
                notice.message_id,
                stamp,
                stamp,
            ),
        )
        return
    conn.execute(
        "UPDATE shopping_orders SET status = ?, total = COALESCE(total, ?), "
        "currency = COALESCE(currency, ?), updated_at = ? WHERE id = ?",
        (later_status(row["status"], notice.status), total, notice.currency, stamp, row["id"]),
    )


def apply_shipment(conn: sqlite3.Connection, notice: Notice, stamp: str) -> list[ShipmentChange]:
    """Create or advance shipment rows for one notice; return the rows that are new or moved forward.

    With tracking numbers, each number is its own shipment; an order's placeholder row (created by the
    order confirmation) takes the first number so its timeline continues. Without tracking numbers,
    every shipment of the order advances, or a placeholder row is created.
    """
    if notice.tracking:
        rows = [_tracked_row(conn, notice, tracking, stamp) for tracking in notice.tracking]
    elif notice.order_id is not None:
        rows = _order_rows(conn, notice, stamp)
    else:
        return []
    changes: list[ShipmentChange] = []
    for row, created in rows:
        moved = _advance(conn, row, notice, stamp)
        if moved or created:
            changes.append(_change(conn, int(row["id"])))
    return changes


def _tracked_row(
    conn: sqlite3.Connection, notice: Notice, tracking: Tracking, stamp: str
) -> tuple[sqlite3.Row, bool]:
    key = "track:" + tracking.number
    row = _find(conn, key)
    if row is not None:
        conn.execute(
            "UPDATE shopping_shipments SET order_id = COALESCE(order_id, ?) WHERE id = ?",
            (notice.order_id, row["id"]),
        )
        return row, False
    placeholder = _placeholder(conn, notice)
    if placeholder is not None:
        conn.execute(
            "UPDATE shopping_shipments SET shipment_key = ?, carrier = ?, tracking_number = ?, "
            "tracking_url = ? WHERE id = ?",
            (key, tracking.carrier, tracking.number, tracking.url, placeholder["id"]),
        )
        return placeholder, False
    _insert(conn, key, notice, tracking, stamp)
    return _fetch(conn, key), True


def _order_rows(conn: sqlite3.Connection, notice: Notice, stamp: str) -> list[tuple[sqlite3.Row, bool]]:
    rows = conn.execute(
        "SELECT id, status FROM shopping_shipments WHERE merchant = ? AND order_id = ? ORDER BY id",
        (notice.merchant, notice.order_id),
    ).fetchall()
    if rows:
        return [(row, False) for row in rows]
    key = f"order:{notice.merchant.lower()}:{notice.order_id}"
    _insert(conn, key, notice, None, stamp)
    return [(_fetch(conn, key), True)]


def _placeholder(conn: sqlite3.Connection, notice: Notice) -> sqlite3.Row | None:
    if notice.order_id is None:
        return None
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, status FROM shopping_shipments "
        "WHERE merchant = ? AND order_id = ? AND tracking_number IS NULL ORDER BY id LIMIT 1",
        (notice.merchant, notice.order_id),
    ).fetchone()
    return row


def _insert(
    conn: sqlite3.Connection, key: str, notice: Notice, tracking: Tracking | None, stamp: str
) -> None:
    conn.execute(
        "INSERT INTO shopping_shipments (shipment_key, merchant, order_id, carrier, tracking_number, "
        "tracking_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            notice.merchant,
            notice.order_id,
            tracking.carrier if tracking else None,
            tracking.number if tracking else None,
            tracking.url if tracking else None,
            notice.status,
            stamp,
            stamp,
        ),
    )


def _advance(conn: sqlite3.Connection, row: sqlite3.Row, notice: Notice, stamp: str) -> bool:
    """Record the notice on the timeline; True when the shipment's status moved forward."""
    current = str(row["status"])
    status = later_status(current, notice.status)
    conn.execute(_ADVANCE[notice.status], (status, stamp, notice.message_id, stamp, row["id"]))
    return status != current


def _find(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, status FROM shopping_shipments WHERE shipment_key = ?", (key,)
    ).fetchone()
    return row


def _fetch(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = _find(conn, key)
    if row is None:
        raise LookupError(f"shipment {key} disappeared during the transaction")
    return row


def _change(conn: sqlite3.Connection, shipment_id: int) -> ShipmentChange:
    row = conn.execute(
        "SELECT shipment_key, merchant, order_id, carrier, tracking_number, tracking_url, status "
        "FROM shopping_shipments WHERE id = ?",
        (shipment_id,),
    ).fetchone()
    return ShipmentChange(
        key=row["shipment_key"],
        merchant=row["merchant"],
        order_id=row["order_id"],
        carrier=row["carrier"],
        tracking_number=row["tracking_number"],
        tracking_url=row["tracking_url"],
        status=row["status"],
    )


def list_orders(
    db: Database, *, status: str | None, merchant: str | None, limit: int
) -> list[dict[str, Any]]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT merchant, order_id, total, currency, status, created_at, updated_at FROM shopping_orders "
            "WHERE (? IS NULL OR status = ?) AND (? IS NULL OR merchant = ?) "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (status, status, merchant, merchant, limit),
        ).fetchall()
    return [
        {
            "merchant": row["merchant"],
            "order_id": row["order_id"],
            "total": row["total"],
            "currency": row["currency"],
            "status": row["status"],
            "first_seen": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def list_shipments(db: Database, *, status: str | None, limit: int) -> list[dict[str, Any]]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT merchant, order_id, carrier, tracking_number, tracking_url, status, ordered_at, "
            "shipped_at, out_for_delivery_at, delivered_at, updated_at FROM shopping_shipments "
            "WHERE (? IS NULL OR status = ?) ORDER BY updated_at DESC, id DESC LIMIT ?",
            (status, status, limit),
        ).fetchall()
    return [
        {
            "merchant": row["merchant"],
            "order_id": row["order_id"],
            "carrier": row["carrier"],
            "tracking_number": row["tracking_number"],
            "tracking_url": row["tracking_url"],
            "status": row["status"],
            "timeline": {name: row[f"{name}_at"] for name in STATUSES},
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


# ---- Price watches -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Watch:
    id: int
    url: str
    title: str | None
    target_price: Decimal
    currency: str
    next_allowed_at: str | None


def add_watch(
    db: Database, *, url: str, target: Decimal, currency: str, note: str | None, now: datetime
) -> tuple[int, bool]:
    """Create a watch, or update and reactivate the one for ``url``; return (id, created).

    Re-adding a URL never resets ``next_allowed_at``, so it cannot be used to fetch a page early.
    """
    stamp = iso(now)
    with db.session() as conn, transaction(conn):
        row = conn.execute("SELECT id FROM shopping_watches WHERE url = ?", (url,)).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE shopping_watches SET target_price = ?, currency = ?, note = ?, active = 1, "
                "updated_at = ? WHERE id = ?",
                (price_text(target), currency, note, stamp, row["id"]),
            )
            return int(row["id"]), False
        active = conn.execute("SELECT COUNT(*) AS n FROM shopping_watches WHERE active = 1").fetchone()
        if int(active["n"]) >= MAX_ACTIVE_WATCHES:
            raise ToolError(f"at most {MAX_ACTIVE_WATCHES} active watches; remove one first")
        conn.execute(
            "INSERT INTO shopping_watches "
            "(url, target_price, currency, note, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (url, price_text(target), currency, note, stamp, stamp),
        )
        created = conn.execute("SELECT id FROM shopping_watches WHERE url = ?", (url,)).fetchone()
        return int(created["id"]), True


def deactivate_watch(db: Database, watch_id: int, now: datetime) -> bool:
    """Stop checking a watch but keep its price history; False when no active watch has that id."""
    with db.session() as conn:
        cursor = conn.execute(
            "UPDATE shopping_watches SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
            (iso(now), watch_id),
        )
        return cursor.rowcount == 1


def list_watches(db: Database, *, include_inactive: bool, limit: int) -> list[dict[str, Any]]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT w.id, w.url, w.title, w.target_price, w.currency, w.note, w.active, w.next_allowed_at, "
            "w.last_checked_at, w.last_result, p.price AS last_price, p.currency AS last_currency, "
            "p.availability AS last_availability, p.observed_at AS last_observed_at "
            "FROM shopping_watches w LEFT JOIN shopping_prices p ON p.id = "
            "(SELECT MAX(id) FROM shopping_prices WHERE watch_id = w.id) "
            "WHERE (? = 1 OR w.active = 1) ORDER BY w.id LIMIT ?",
            (1 if include_inactive else 0, limit),
        ).fetchall()
    return [
        {
            "watch_id": row["id"],
            "url": row["url"],
            "title": row["title"],
            "target_price": row["target_price"],
            "currency": row["currency"],
            "note": row["note"],
            "active": bool(row["active"]),
            "last_price": row["last_price"],
            "last_price_currency": row["last_currency"],
            "last_availability": row["last_availability"],
            "last_observed_at": row["last_observed_at"],
            "last_checked_at": row["last_checked_at"],
            "last_result": row["last_result"],
            "next_allowed_at": row["next_allowed_at"],
        }
        for row in rows
    ]


def due_watches(db: Database, now: datetime, limit: int) -> list[Watch]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, url, title, target_price, currency, next_allowed_at FROM shopping_watches "
            "WHERE active = 1 AND (next_allowed_at IS NULL OR next_allowed_at <= ?) "
            "ORDER BY COALESCE(next_allowed_at, ''), id LIMIT ?",
            (iso(now), limit),
        ).fetchall()
    return [
        Watch(
            id=int(row["id"]),
            url=str(row["url"]),
            title=row["title"],
            target_price=Decimal(row["target_price"]),
            currency=str(row["currency"]),
            next_allowed_at=row["next_allowed_at"],
        )
        for row in rows
    ]


def claim_watch(db: Database, watch: Watch, *, now: datetime, until: datetime) -> bool:
    """Reserve a due watch before fetching, so two processes never fetch the same URL at once."""
    with db.session() as conn:
        cursor = conn.execute(
            "UPDATE shopping_watches SET next_allowed_at = ? "
            "WHERE id = ? AND active = 1 AND (next_allowed_at IS NULL OR next_allowed_at <= ?)",
            (iso(until), watch.id, iso(now)),
        )
        return cursor.rowcount == 1


def finish_check(
    db: Database, watch_id: int, *, now: datetime, next_allowed_at: datetime, result: str
) -> None:
    with db.session() as conn:
        conn.execute(
            "UPDATE shopping_watches SET last_checked_at = ?, last_result = ?, next_allowed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (iso(now), result, iso(next_allowed_at), iso(now), watch_id),
        )


def save_observation(
    db: Database, watch_id: int, found: PriceFound, *, now: datetime, next_allowed_at: datetime
) -> Decimal | None:
    """Store one observed price and finish the check; return the previous observed price, if any."""
    stamp = iso(now)
    with db.session() as conn, transaction(conn):
        previous = conn.execute(
            "SELECT price FROM shopping_prices WHERE watch_id = ? ORDER BY id DESC LIMIT 1", (watch_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO shopping_prices (watch_id, observed_at, price, currency, availability, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (watch_id, stamp, price_text(found.price), found.currency, found.availability, found.source),
        )
        conn.execute(
            "UPDATE shopping_watches SET title = COALESCE(title, ?), last_checked_at = ?, "
            "last_result = 'price', next_allowed_at = ?, updated_at = ? WHERE id = ?",
            (found.title, stamp, iso(next_allowed_at), stamp, watch_id),
        )
    return Decimal(previous["price"]) if previous is not None else None
