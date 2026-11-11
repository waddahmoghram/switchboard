"""Durable polling snapshots and event enqueueing in the same local transaction."""

from __future__ import annotations

from typing import Any

from switchboard.connectors.home.settings import HomeSettings
from switchboard.connectors.home.state import comparable
from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import canonical_json, json_equal, json_loads

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE home_snapshots (
                source TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                PRIMARY KEY (source, entity_id)
            )
            """,
            """
            CREATE TABLE home_poll_status (
                source TEXT PRIMARY KEY,
                attempted_at TEXT NOT NULL,
                succeeded_at TEXT,
                error TEXT
            )
            """,
        ),
    ),
]


class HomeStore:
    def __init__(self, ctx: AppContext, settings: HomeSettings) -> None:
        self.ctx = ctx
        self.settings = settings

    def status(self) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            row = conn.execute(
                "SELECT attempted_at, succeeded_at, error FROM home_poll_status WHERE source = ?",
                (self.settings.source,),
            ).fetchone()
        return dict(row) if row is not None else {"attempted_at": None, "succeeded_at": None, "error": None}

    def snapshots(self) -> list[dict[str, Any]]:
        with self.ctx.db.session() as conn:
            rows = conn.execute(
                "SELECT entity_id, state_json, revision FROM home_snapshots "
                "WHERE source = ? ORDER BY entity_id",
                (self.settings.source,),
            ).fetchall()
        allowed = set(self.settings.poll.entities)
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            if row["entity_id"] in allowed and self.settings.visible(row["entity_id"]):
                snapshot = _decode(row["state_json"])
                snapshot["revision"] = row["revision"]
                snapshots.append(snapshot)
        return snapshots

    def success(self, snapshots: list[dict[str, Any]], attempted_at: str, observed_at: str) -> dict[str, Any]:
        baseline = changed = 0
        source = self.settings.source
        with self.ctx.db.session() as conn, transaction(conn):
            latest = conn.execute(
                "SELECT attempted_at FROM home_poll_status WHERE source = ?", (source,)
            ).fetchone()
            if latest is not None and latest["attempted_at"] > attempted_at:
                return {"baselined": 0, "changed": 0, "ignored_older_sample": True}
            for snapshot in snapshots:
                entity_id = snapshot["entity_id"]
                row = conn.execute(
                    "SELECT state_json, revision FROM home_snapshots WHERE source = ? AND entity_id = ?",
                    (source, entity_id),
                ).fetchone()
                previous = _decode(row["state_json"]) if row is not None else None
                different = previous is not None and not json_equal(
                    comparable(previous), comparable(snapshot)
                )
                revision = int(row["revision"]) + int(different) if row is not None else 1
                if previous is None:
                    baseline += 1
                elif different:
                    self.ctx.events.emit(
                        "home",
                        "home.state_changed",
                        {
                            "entity_id": entity_id,
                            "previous": previous,
                            "current": snapshot,
                            "observed_at": observed_at,
                            "revision": revision,
                            "source": source,
                            "history_complete": False,
                        },
                        dedupe_key=f"home:{source}:{entity_id}:{revision}",
                        connection=conn,
                    )
                    changed += 1
                conn.execute(
                    "INSERT INTO home_snapshots (source, entity_id, state_json, observed_at, revision) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, entity_id) DO UPDATE SET "
                    "state_json = excluded.state_json, observed_at = excluded.observed_at, "
                    "revision = excluded.revision",
                    (source, entity_id, canonical_json(snapshot), observed_at, revision),
                )
            conn.execute(
                "INSERT INTO home_poll_status (source, attempted_at, succeeded_at, error) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(source) DO UPDATE SET attempted_at = excluded.attempted_at, "
                "succeeded_at = excluded.succeeded_at, error = NULL",
                (source, attempted_at, observed_at),
            )
        return {"baselined": baseline, "changed": changed, "ignored_older_sample": False}

    def failure(self, attempted_at: str) -> None:
        with self.ctx.db.session() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO home_poll_status (source, attempted_at, error) VALUES (?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET attempted_at = excluded.attempted_at, "
                "error = excluded.error "
                "WHERE excluded.attempted_at >= home_poll_status.attempted_at",
                (self.settings.source, attempted_at, "poll failed; previous snapshots retained"),
            )


def _decode(value: str) -> dict[str, Any]:
    try:
        result = json_loads(value)
    except ValueError as exc:
        raise ToolError("the stored home snapshot is unreadable") from exc
    if not isinstance(result, dict):
        raise ToolError("the stored home snapshot is malformed")
    return result
