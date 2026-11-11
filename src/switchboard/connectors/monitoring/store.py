"""Atomic snapshots and bounded transition events in the core event queue."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.util import canonical_json, sha256_hex

from .checks import Observation
from .settings import Check, configuration_key

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            "CREATE TABLE monitoring_snapshots ("
            "name TEXT PRIMARY KEY, config_key TEXT NOT NULL, state_key TEXT NOT NULL, "
            "snapshot_json TEXT NOT NULL, checked_at TEXT NOT NULL, changed_at TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK (revision >= 1))",
        ),
    ),
]
ALERT_STATUSES = frozenset({"unhealthy", "stale", "error"})


@dataclass(frozen=True)
class Saved:
    snapshot: dict[str, Any]
    changed: bool
    events_queued: int


class SnapshotStore:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def get(self, check: Check) -> dict[str, Any] | None:
        with self.ctx.db.session() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM monitoring_snapshots WHERE name = ? AND config_key = ?",
                (check.name, configuration_key(check)),
            ).fetchone()
        return None if row is None else json.loads(row["snapshot_json"])

    def record(self, check: Check, observation: Observation, checked_at: str) -> Saved:
        """Commit a snapshot and all its transition events, or none of them."""
        config_key = configuration_key(check)
        state = {
            "kind": check.kind,
            "status": observation.status,
            "error": observation.error,
            "reachable": observation.reachable,
            "ready": observation.ready,
            "report_status": observation.report.get("status"),
        }
        state_key = sha256_hex(canonical_json(state))
        with self.ctx.db.session() as conn, transaction(conn):
            previous = conn.execute(
                "SELECT config_key, state_key, snapshot_json, checked_at, changed_at, revision "
                "FROM monitoring_snapshots WHERE name = ?",
                (check.name,),
            ).fetchone()
            if (
                previous is not None
                and previous["config_key"] == config_key
                and previous["checked_at"] > checked_at
            ):
                return Saved(json.loads(previous["snapshot_json"]), False, 0)
            changed = (
                previous is None or previous["config_key"] != config_key or previous["state_key"] != state_key
            )
            revision = (0 if previous is None else int(previous["revision"])) + int(changed)
            changed_at = checked_at if changed or previous is None else previous["changed_at"]
            snapshot = {
                "name": check.name,
                "kind": check.kind,
                **asdict(observation),
                "checked_at": checked_at,
                "changed_at": changed_at,
                "revision": revision,
            }
            conn.execute(
                "INSERT INTO monitoring_snapshots "
                "(name, config_key, state_key, snapshot_json, checked_at, changed_at, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "config_key = excluded.config_key, state_key = excluded.state_key, "
                "snapshot_json = excluded.snapshot_json, checked_at = excluded.checked_at, "
                "changed_at = excluded.changed_at, revision = excluded.revision",
                (
                    check.name,
                    config_key,
                    state_key,
                    canonical_json(snapshot),
                    checked_at,
                    changed_at,
                    revision,
                ),
            )
            queued = 0
            if changed:
                old = None if previous is None else json.loads(previous["snapshot_json"])
                payload = {
                    **snapshot,
                    "previous_status": (
                        old["status"]
                        if old is not None and previous is not None and previous["config_key"] == config_key
                        else None
                    ),
                }
                for event_type in ("monitoring.changed", "monitoring.alert"):
                    if event_type == "monitoring.alert" and observation.status not in ALERT_STATUSES:
                        continue
                    emitted = self.ctx.events.emit(
                        "monitoring",
                        event_type,
                        payload,
                        dedupe_key=f"monitoring:{check.name}:{revision}:{event_type}",
                        connection=conn,
                    )
                    queued += int(emitted is not None)
        return Saved(snapshot, changed, queued)
