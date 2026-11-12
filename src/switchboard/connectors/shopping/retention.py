"""Opt-in retention for price-watch history, enabled by ``[retention.connectors] shopping = <days>``.

Observations older than the cutoff are deleted, except each watch's newest one: price-drop detection and the
watch listing compare against it. Orders, shipments, and watches are kept, because deleting them could let a
re-read email recreate a parcel and announce it again.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

EXPORT_TABLES = ("shopping_orders", "shopping_shipments", "shopping_watches", "shopping_prices")


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        removed = conn.execute(
            "DELETE FROM shopping_prices WHERE observed_at < ? AND id < "
            "(SELECT MAX(newest.id) FROM shopping_prices AS newest "
            "WHERE newest.watch_id = shopping_prices.watch_id)",
            (iso(older_than),),
        ).rowcount
    return {"shopping_prices": removed}
