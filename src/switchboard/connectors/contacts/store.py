"""Cache of cards (table ``contacts_cards``) and per-source fetch state (core ``kv`` table).

Only what search returns is stored: the display name, email addresses, phone numbers, and
organization. Notes and birthdays are parsed but never cached.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from switchboard.connectors.contacts.vcard import Card, Entry
from switchboard.db import Database, Migration, transaction
from switchboard.util import canonical_json, iso

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE contacts_cards (
                source TEXT NOT NULL,
                uid TEXT NOT NULL,
                fn TEXT NOT NULL,
                emails_json TEXT NOT NULL,
                phones_json TEXT NOT NULL,
                org TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, uid)
            )
            """,
        ),
    ),
]


def _entries(entries: Sequence[Entry]) -> str:
    return canonical_json([{"value": entry.value, "types": list(entry.types)} for entry in entries])


class CardStore:
    def __init__(self, db: Database, namespace: str) -> None:
        self.db = db
        self.namespace = namespace

    def replace(self, source: str, cards: Iterable[Card], now: datetime) -> int:
        """Swap in a source's cards in one transaction; returns how many rows were written."""
        stamp = iso(now)
        rows = [
            (source, card.uid, card.fn, _entries(card.emails), _entries(card.phones), card.org, stamp)
            for card in cards
        ]
        with self.db.session() as conn, transaction(conn):
            conn.execute("DELETE FROM contacts_cards WHERE source = ?", (source,))
            conn.executemany(
                "INSERT OR REPLACE INTO contacts_cards "
                "(source, uid, fn, emails_json, phones_json, org, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def cards(self, sources: Sequence[str]) -> list[dict[str, Any]]:
        """Cached cards of the given sources as plain dictionaries."""
        found: list[dict[str, Any]] = []
        with self.db.session() as conn:
            for source in sources:
                rows = conn.execute(
                    "SELECT uid, fn, emails_json, phones_json, org FROM contacts_cards WHERE source = ?",
                    (source,),
                ).fetchall()
                found += [
                    {
                        "source": source,
                        "uid": row["uid"],
                        "fn": row["fn"],
                        "emails": json.loads(row["emails_json"]),
                        "phones": json.loads(row["phones_json"]),
                        "org": row["org"],
                    }
                    for row in rows
                ]
        return found

    def state(self, source: str) -> dict[str, Any]:
        raw = self.db.kv_get(self.namespace, f"source:{source}")
        return {} if raw is None else dict(json.loads(raw))

    def save_state(self, source: str, state: Mapping[str, Any]) -> None:
        self.db.kv_set(self.namespace, f"source:{source}", canonical_json(dict(state)))
