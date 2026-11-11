"""A named-check service: construction is offline, and cached status never measures a host."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.util import now

from .checks import measure
from .settings import BackupCheck, Check, Settings
from .store import Saved, SnapshotStore


class MonitoringService:
    def __init__(self, ctx: AppContext, settings: Settings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.checks = {check.name: check for check in settings.checks}
        self.store = SnapshotStore(ctx)
        self.clock: Callable[[], datetime] = now

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read only configured snapshots, marking expired observations without taking measurements."""
        targets = self._targets(args, required=False)
        stamp = self._now()
        snapshots = []
        for check in targets:
            saved = self.store.get(check)
            if saved is None:
                saved = {
                    "name": check.name,
                    "kind": check.kind,
                    "status": "unchecked",
                    "error": "not_checked",
                    "reachable": None,
                    "ready": None,
                    "measurements": {},
                    "report": {},
                    "checked_at": None,
                    "changed_at": None,
                    "revision": 0,
                }
            snapshots.append(self._render(saved, stamp, check))
        return {
            "checks": snapshots,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "snapshot_stale_after_seconds": self.settings.snapshot_stale_after_seconds,
        }

    def check(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run one named read-only check, never an address, path, or command supplied by a caller."""
        [target] = self._targets(args, required=True)
        saved = self._run(target)
        return {
            "check": self._render(saved.snapshot, self._now(), target),
            "changed": saved.changed,
            "events_queued": saved.events_queued,
        }

    def poll(self) -> dict[str, int]:
        """Attempt every configured check even when one observation cannot be committed."""
        report = {"checked": 0, "changed": 0, "events_queued": 0, "record_errors": 0}
        for target in self.settings.checks:
            try:
                saved = self._run(target)
            except Exception:
                report["record_errors"] += 1
                continue
            report["checked"] += 1
            report["changed"] += int(saved.changed)
            report["events_queued"] += saved.events_queued
        return report

    def poll_job(self) -> None:
        if self.poll()["record_errors"]:
            raise ToolError("monitoring: some observations could not be stored; other checks were attempted")

    def _run(self, check: Check) -> Saved:
        self._now()
        observation = measure(self.ctx, check, self._now)
        stamp = self._now()
        rendered = stamp.astimezone(UTC).isoformat(timespec="microseconds")
        return self.store.record(check, observation, rendered)

    def _now(self) -> datetime:
        stamp = self.clock()
        if stamp.utcoffset() is None:
            raise ToolError("monitoring: the observation clock must be timezone aware")
        return stamp

    def _targets(self, args: dict[str, Any], *, required: bool) -> list[Check]:
        if set(args) - {"name"}:
            raise ToolError("monitoring: only a configured check name is accepted")
        name = args.get("name")
        if "name" not in args and not required:
            return list(self.settings.checks)
        if not isinstance(name, str) or name not in self.checks:
            raise ToolError("monitoring: choose a configured check name")
        return [self.checks[name]]

    def _render(self, snapshot: dict[str, Any], stamp: datetime, check: Check) -> dict[str, Any]:
        data = dict(snapshot)
        checked_at = data["checked_at"]
        age = None if checked_at is None else (stamp - datetime.fromisoformat(checked_at)).total_seconds()
        expired = age is not None and (age < 0 or age > self.settings.snapshot_stale_after_seconds)
        data["observed_status"] = data["status"]
        data["observed_error"] = data["error"]
        data["snapshot_stale"] = expired
        finished_at = data["report"].get("finished_at")
        report_stale = (
            isinstance(check, BackupCheck)
            and finished_at is not None
            and (stamp - datetime.fromisoformat(finished_at)).total_seconds() > check.max_age_seconds
        )
        data["report_stale"] = report_stale
        if report_stale:
            data["status"] = "stale"
            data["error"] = "backup_report_stale"
        if expired:
            data["status"] = "stale"
            data["error"] = "snapshot_stale"
        return data
