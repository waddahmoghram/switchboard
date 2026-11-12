"""Opt-in retention for the Telegram inbox, enabled by ``[retention.connectors] telegram = <days>``.

Whole stored messages older than the cutoff are deleted, including chat, sender, and reply metadata. The
connector's own ``retention_days`` setting only blanks message text. Messages whose reply is still being sent
(``sending``) or has an unknown delivery outcome (``unknown``) are kept, so the single-reply guard still
refuses a blind second reply. Telegram re-delivers updates only for about a day, so a deleted old message is
not re-imported.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.context import AppContext
from switchboard.lifecycle import purge_transaction
from switchboard.util import iso

EXPORT_TABLES = ("telegram_messages",)


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    with ctx.db.session() as conn, purge_transaction(conn, dry_run=dry_run):
        removed = conn.execute(
            "DELETE FROM telegram_messages WHERE received_at < ? "
            "AND reply_state NOT IN ('sending', 'unknown')",
            (iso(older_than),),
        ).rowcount
    return {"telegram_messages": removed}
