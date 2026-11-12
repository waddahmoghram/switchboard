"""Opt-in retention for imported WhatsApp exports, enabled by ``[retention.connectors] whatsapp = <days>``.

Imported records and import markers older than the cutoff are deleted. A record is dated by the import that
first stored it: a later import of identical content does not refresh that date. Removing the marker lets the
same export file be imported again deliberately. Export files in the import folder are not touched.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

EXPORT_TABLES = ("whatsapp_imports", "whatsapp_records")


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    cutoff = iso(older_than)
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        records = conn.execute("DELETE FROM whatsapp_records WHERE imported_at < ?", (cutoff,)).rowcount
        imports = conn.execute("DELETE FROM whatsapp_imports WHERE imported_at < ?", (cutoff,)).rowcount
    return {"whatsapp_records": records, "whatsapp_imports": imports}
