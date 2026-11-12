"""Opt-in retention for imported LinkedIn exports, enabled by ``[retention.connectors] linkedin = <days>``.

Imported records and import markers older than the cutoff are deleted. A record is dated by the import that
first stored it: a later import of identical content does not refresh that date. Removing the marker lets the
same export file be imported again deliberately. Local post drafts and publication attempts are never purged
here, so an in-flight or unknown publication keeps its guard.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

EXPORT_TABLES = ("linkedin_imports", "linkedin_records", "linkedin_drafts", "linkedin_publish_attempts")


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    cutoff = iso(older_than)
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        records = conn.execute("DELETE FROM linkedin_records WHERE imported_at < ?", (cutoff,)).rowcount
        imports = conn.execute("DELETE FROM linkedin_imports WHERE imported_at < ?", (cutoff,)).rowcount
    return {"linkedin_records": records, "linkedin_imports": imports}
