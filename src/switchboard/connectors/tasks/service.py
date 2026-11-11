"""Task input normalization and reminder scheduling, without a cloud task account."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from switchboard.context import AppContext
from switchboard.errors import SwitchboardError, ToolError
from switchboard.util import canonical_json, iso, json_loads, now

from .settings import Settings
from .store import TaskStore


def normalize(fields: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(fields)
    for key in ("title", "list_name"):
        if key in result:
            if not isinstance(result[key], str) or not result[key].strip():
                raise ToolError(f"{key} must not be blank")
            result[key] = result[key].strip()
    for key in ("due_at", "remind_at"):
        value = result.get(key)
        if value is None:
            continue
        try:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("missing timezone")
            result[key] = iso(moment)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ToolError(f"{key} must be an ISO 8601 timestamp with an explicit timezone offset") from exc
    return result


class TaskService:
    def __init__(self, ctx: AppContext, settings: Settings, *, clock: Callable[[], datetime] = now) -> None:
        self.ctx = ctx
        self.settings = settings
        self.clock = clock
        self.store = TaskStore(ctx)

    def add(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.create(normalize(args), self.clock())

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.read(args["id"])

    def listing(self, args: dict[str, Any]) -> dict[str, Any]:
        fields = normalize(args)
        return self.store.search(
            status=fields.get("status", "open"),
            query=fields.get("query", ""),
            list_name=fields.get("list_name"),
            after_id=fields.get("after_id", 0),
            limit=fields.get("limit", 20),
            overdue=fields.get("overdue", False),
            moment=self.clock(),
        )

    def update(self, args: dict[str, Any]) -> dict[str, Any]:
        fields = normalize({key: value for key, value in args.items() if key not in {"id", "revision"}})
        return self.store.update(args["id"], args["revision"], fields, self.clock())

    def delete(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.delete(args["id"], args["revision"], self.clock())

    def status(self, _args: dict[str, Any]) -> dict[str, Any]:
        job = next(item for item in self.ctx.scheduler.jobs() if item.name == "tasks.reminders")
        raw = self.ctx.db.kv_get("tasks", "reminders")
        try:
            check = json_loads(raw) if raw is not None else None
        except ValueError as exc:
            raise ToolError("the stored reminder-check state is damaged") from exc
        if check is not None:
            if (
                not isinstance(check, dict)
                or set(check) != {"checked_at", "queued", "error"}
                or not isinstance(check["checked_at"], str)
                or not 1 <= len(check["checked_at"]) <= 64
                or (
                    check["queued"] is not None
                    and (type(check["queued"]) is not int or not 0 <= check["queued"] <= 500)
                )
                or (check["error"] is not None and not isinstance(check["error"], str))
                or ((check["error"] is None) != (check["queued"] is not None))
            ):
                raise ToolError("the stored reminder-check state is damaged")
            try:
                normalize({"remind_at": check["checked_at"]})
            except ToolError as exc:
                raise ToolError("the stored reminder-check state is damaged") from exc
        return {
            "counts": self.store.counts(),
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "job_error": job.last_error,
            "last_check": check,
            "delivery": "events_only; configure notification rules",
        }

    def reminders_job(self) -> None:
        moment = self.clock()
        stamp = iso(moment)
        try:
            queued = self.store.queue_due(moment, self.settings.max_reminders_per_tick)
        except (sqlite3.Error, SwitchboardError) as exc:
            self.ctx.db.kv_set(
                "tasks",
                "reminders",
                canonical_json({"checked_at": stamp, "queued": None, "error": type(exc).__name__}),
            )
            raise
        self.ctx.db.kv_set(
            "tasks",
            "reminders",
            canonical_json({"checked_at": stamp, "queued": queued, "error": None}),
        )
