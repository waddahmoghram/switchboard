"""Durable event queue backed by SQLite.

Connectors *emit* events (``mail.received``, ``webhook.received``, ``finance.transaction`` …).
Connector subscribers process them first (for example, finance turns bank-alert emails into ledger
entries and emits derived events), then the rule engine runs your automations. Queued events survive
restarts, but subscriber effects and event completion are not a single transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from switchboard.db import Database
from switchboard.util import canonical_json, glob_any, iso_before, now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    id: int
    source: str
    type: str
    payload: dict[str, Any]
    created_at: str


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._subscribers: list[tuple[str, str, Subscriber]] = []

    def emit(
        self,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int | None:
        """Queue an event, optionally in the caller's transaction; None means an existing dedupe key."""
        if connection is not None:
            return self._insert(connection, source, event_type, payload, dedupe_key)
        with self.db.session() as conn:
            return self._insert(conn, source, event_type, payload, dedupe_key)

    @staticmethod
    def _insert(
        conn: sqlite3.Connection,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        dedupe_key: str | None,
    ) -> int | None:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO events (source, type, payload_json, dedupe_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, event_type, canonical_json(payload), dedupe_key, now_iso()),
        )
        return cursor.lastrowid if cursor.rowcount == 1 else None

    def subscribe(self, pattern: str, name: str, handler: Subscriber) -> None:
        """Run ``handler`` for every event whose type matches the glob ``pattern``."""
        self._subscribers.append((pattern, name, handler))

    def pending(self, limit: int = 100) -> list[Event]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT id, source, type, payload_json, created_at FROM events "
                "WHERE processed_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Event(row["id"], row["source"], row["type"], json.loads(row["payload_json"]), row["created_at"])
            for row in rows
        ]

    def recent(
        self, limit: int = 20, *, event_type: str | None = None, source: str | None = None
    ) -> list[Event]:
        """Newest events first, optionally filtered by exact type and source."""
        query = "SELECT id, source, type, payload_json, created_at FROM events"
        clauses: list[str] = []
        params: list[object] = []
        if event_type is not None:
            clauses.append("type = ?")
            params.append(event_type)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.db.session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            Event(row["id"], row["source"], row["type"], json.loads(row["payload_json"]), row["created_at"])
            for row in rows
        ]

    def mark(self, event_id: int, error: str | None = None) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE events SET processed_at = ?, error = ? WHERE id = ?",
                (now_iso(), error, event_id),
            )

    def failures(self, limit: int = 20) -> list[dict[str, Any]]:
        """Failure metadata without payloads; processing failures are not automatically retried."""
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT id, source, type, created_at, processed_at, error FROM events "
                "WHERE error IS NOT NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def process(self, after: Subscriber | None = None, *, limit: int = 100, max_rounds: int = 5) -> int:
        """Deliver pending events to subscribers, then to ``after`` (the rule engine).

        Derived events emitted while processing are picked up in later rounds. A failing handler is
        logged and recorded on the event; it does not stop other handlers. Events are not retried.
        """
        handled = 0
        for _ in range(max_rounds):
            batch = self.pending(limit)
            if not batch:
                break
            for event in batch:
                problems: list[str] = []
                for pattern, name, handler in self._subscribers:
                    if glob_any(event.type, [pattern]):
                        try:
                            handler(event)
                        except Exception as exc:
                            log.exception("subscriber %s failed on event %s", name, event.id)
                            problems.append(f"{name}: {type(exc).__name__}")
                if after is not None:
                    try:
                        after(event)
                    except Exception as exc:
                        log.exception("rules failed on event %s", event.id)
                        problems.append(f"rules: {type(exc).__name__}")
                self.mark(event.id, "; ".join(problems) or None)
                handled += 1
        return handled

    def prune(self, older_than_days: int) -> int:
        """Remove old processed events; return how many were deleted or emptied.

        Events without a deduplication key are deleted. Events with one keep their row and key, but their
        payload is replaced by ``{}``: the data goes, while a re-delivered message stays a duplicate.
        """
        cutoff = iso_before(older_than_days * 86400)
        with self.db.session() as conn:
            deleted = conn.execute(
                "DELETE FROM events WHERE processed_at IS NOT NULL AND created_at < ? AND dedupe_key IS NULL",
                (cutoff,),
            ).rowcount
            emptied = conn.execute(
                "UPDATE events SET payload_json = '{}' WHERE processed_at IS NOT NULL AND created_at < ? "
                "AND dedupe_key IS NOT NULL AND payload_json != '{}'",
                (cutoff,),
            ).rowcount
        return deleted + emptied
