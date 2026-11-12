"""Opt-in retention for stored webhook bodies, enabled by ``[retention.connectors] webhooks = <days>``.

Verified deliveries are stored as ``webhook.received`` events. Processed deliveries older than the cutoff
are deleted, or emptied to ``{}`` when they carry a deduplication key, which stays so a re-delivered
notification is still recognized as a duplicate. Unprocessed deliveries are kept for the runner. Replay-cache
entries that expired before the cutoff are removed too. The core ``[switchboard] event_retention_days``
still applies; this hook lets webhook bodies go sooner. Bodies are not exported here: ``data export``
already includes every event.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.connectors.webhooks.receiver import EVENT_TYPE
from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

SOURCE = "webhooks"
EXPORT_TABLES: tuple[str, ...] = ()


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    cutoff = iso(older_than)
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        deleted = conn.execute(
            "DELETE FROM events WHERE source = ? AND type = ? AND processed_at IS NOT NULL "
            "AND created_at < ? AND dedupe_key IS NULL",
            (SOURCE, EVENT_TYPE, cutoff),
        ).rowcount
        emptied = conn.execute(
            "UPDATE events SET payload_json = '{}' WHERE source = ? AND type = ? "
            "AND processed_at IS NOT NULL AND created_at < ? AND dedupe_key IS NOT NULL "
            "AND payload_json != '{}'",
            (SOURCE, EVENT_TYPE, cutoff),
        ).rowcount
        expired = conn.execute(
            "DELETE FROM webhooks_seen WHERE expires_at < ?", (int(older_than.timestamp()),)
        ).rowcount
    return {"webhook_events_deleted": deleted, "webhook_events_emptied": emptied, "webhooks_seen": expired}
