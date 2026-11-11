"""Cache of parsed events (table ``calendar_events``) and per-source fetch state (core ``kv`` table).

Each refresh replaces a source's rows in one transaction, so readers never see half a calendar. Rows
keep the parsed :class:`~switchboard.connectors.calendar.ics.Event` as JSON; ``start_utc`` and
``end_utc`` let a window query skip single events that cannot overlap, while recurring masters and
their overrides are always loaded and expanded in Python.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from switchboard.connectors.calendar.ics import Event
from switchboard.db import Database, Migration, transaction
from switchboard.util import canonical_json, iso

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE calendar_events (
                source TEXT NOT NULL,
                uid TEXT NOT NULL,
                recurrence_id TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                recurring INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (source, uid, recurrence_id)
            )
            """,
            "CREATE INDEX calendar_events_start ON calendar_events(source, start_utc)",
        ),
    ),
]

_WINDOW_QUERY = (
    "SELECT event_json FROM calendar_events WHERE source = ? "
    "AND (recurring = 1 OR recurrence_id != '' OR (start_utc < ? AND end_utc >= ?))"
)


class EventStore:
    def __init__(self, db: Database, namespace: str) -> None:
        self.db = db
        self.namespace = namespace

    def replace(self, source: str, events: Iterable[Event]) -> int:
        """Swap in a source's freshly parsed events; returns how many rows were written."""
        rows = [
            (
                source,
                event.uid,
                "" if event.recurrence_id is None else iso(event.recurrence_id),
                iso(event.start),
                iso(event.end),
                int(bool(event.rrule)),
                canonical_json(event.to_json()),
            )
            for event in events
        ]
        with self.db.session() as conn, transaction(conn):
            conn.execute("DELETE FROM calendar_events WHERE source = ?", (source,))
            conn.executemany(
                "INSERT OR REPLACE INTO calendar_events "
                "(source, uid, recurrence_id, start_utc, end_utc, recurring, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def load(self, sources: Sequence[str], start: datetime, end: datetime) -> dict[str, list[Event]]:
        """Events of each source that may have an instance in ``[start, end)``."""
        found: dict[str, list[Event]] = {}
        with self.db.session() as conn:
            for source in sources:
                rows = conn.execute(_WINDOW_QUERY, (source, iso(end), iso(start))).fetchall()
                found[source] = [Event.from_json(json.loads(row["event_json"])) for row in rows]
        return found

    def state(self, source: str) -> dict[str, Any]:
        raw = self.db.kv_get(self.namespace, f"source:{source}")
        return {} if raw is None else dict(json.loads(raw))

    def save_state(self, source: str, state: Mapping[str, Any]) -> None:
        self.db.kv_set(self.namespace, f"source:{source}", canonical_json(dict(state)))
