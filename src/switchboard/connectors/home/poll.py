"""Opt-in sampling, not a subscription to every Home Assistant state transition."""

from __future__ import annotations

from typing import Any

from switchboard.connectors.home.client import HomeAssistant
from switchboard.connectors.home.settings import HomeSettings
from switchboard.connectors.home.state import observation, stale
from switchboard.connectors.home.store import HomeStore
from switchboard.errors import ToolError
from switchboard.util import now


class HomePoller:
    def __init__(self, client: HomeAssistant, settings: HomeSettings, store: HomeStore) -> None:
        self.client = client
        self.settings = settings
        self.store = store

    def poll(self) -> dict[str, Any]:
        if not self.settings.poll.enabled:
            raise ToolError("home polling is disabled; enable poll with exact exposed entities first")
        attempted_at = now().isoformat(timespec="microseconds")
        try:
            states = self.client.states(strict=True)
            indexed: dict[str, dict[str, Any]] = {}
            for state in states:
                entity_id = state["entity_id"]
                if entity_id in self.settings.poll.entities:
                    if entity_id in indexed:
                        raise ToolError("Home Assistant returned a duplicate polled entity")
                    indexed[entity_id] = state
            observed_at = now().isoformat(timespec="microseconds")
            snapshots = [
                observation(entity_id, indexed.get(entity_id), observed_at)
                for entity_id in self.settings.poll.entities
            ]
        except ToolError as exc:
            self.store.failure(attempted_at)
            raise ToolError(
                "home polling failed; cached snapshots were retained; inspect home_poll_status"
            ) from exc
        result = self.store.success(snapshots, attempted_at, observed_at)
        return {"entities": len(snapshots), **result, "history_complete": False}

    def status(self) -> dict[str, Any]:
        moment = now()
        health = self.store.status()
        snapshots = self.store.snapshots()
        by_id = {snapshot["entity_id"]: snapshot for snapshot in snapshots}
        entities: list[dict[str, Any]] = []
        for entity_id in self.settings.poll.entities:
            snapshot = by_id.get(entity_id)
            if snapshot is None:
                entities.append({"entity_id": entity_id, "availability": "not_observed", "stale": True})
            else:
                entities.append(
                    {
                        **snapshot,
                        "stale": stale(
                            snapshot["observed_at"], moment, self.settings.poll.stale_after_seconds
                        ),
                    }
                )
        return {
            "enabled": self.settings.poll.enabled,
            "interval_seconds": self.settings.poll.interval_seconds,
            "stale_after_seconds": self.settings.poll.stale_after_seconds,
            **health,
            "stale": stale(health["succeeded_at"], moment, self.settings.poll.stale_after_seconds),
            "entities": entities,
            "history_complete": False,
            "freshness_basis": "time of local state reads, not proof of device freshness",
        }
