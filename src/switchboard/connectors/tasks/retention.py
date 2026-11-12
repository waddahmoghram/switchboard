"""Opt-in retention for local tasks, enabled by ``[retention.connectors] tasks = <days>``.

Completed and cancelled tasks whose last change is older than the cutoff are deleted. Open tasks are never
purged, however old. Purging emits no ``tasks.deleted`` events: retention is housekeeping, not a decision a
rule should react to. A pending approval that names a purged task fails without effect.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

EXPORT_TABLES = ("tasks_items",)


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        removed = conn.execute(
            "DELETE FROM tasks_items WHERE status IN ('completed', 'cancelled') AND updated_at < ?",
            (iso(older_than),),
        ).rowcount
    return {"tasks_items": removed}
