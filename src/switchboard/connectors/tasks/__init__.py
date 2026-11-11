"""Personal tasks and one-shot reminder events in the private Switchboard database."""

from __future__ import annotations

from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ConfigError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry

from .service import TaskService
from .settings import load_settings
from .store import MIGRATIONS as MIGRATIONS

NAME = "tasks"
_ID = {"type": "integer", "minimum": 1, "maximum": 2**53 - 1}
_STAMP = {"type": ["string", "null"], "minLength": 1, "maxLength": 64}
_TEXT_FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 200},
    "notes": {"type": "string", "maxLength": 4000},
    "list_name": {"type": "string", "minLength": 1, "maxLength": 64},
    "due_at": _STAMP,
    "remind_at": _STAMP,
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    settings = load_settings(ctx.connector_config(NAME))
    service = TaskService(ctx, settings)
    ctx.provide("tasks.local", service)
    ctx.scheduler.add("tasks.reminders", settings.poll_interval_seconds, service.reminders_job)
    registry.add(
        Tool(
            "tasks_status",
            "Show local task counts and reminder-runner errors; no external provider probe.",
            Tier.READ,
            _schema({}),
            service.status,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "tasks_list",
            "List local tasks as untrusted data; literal search, bounded notes previews, id paging.",
            Tier.READ,
            _schema(
                {
                    "status": {"type": "string", "enum": ["open", "completed", "cancelled", "all"]},
                    "query": {"type": "string", "maxLength": 200},
                    "list_name": _TEXT_FIELDS["list_name"],
                    "after_id": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "overdue": {"type": "boolean"},
                }
            ),
            service.listing,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "tasks_read",
            "Read one local task, including its current revision and untrusted notes.",
            Tier.READ,
            _schema({"id": _ID}, ["id"]),
            service.read,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "tasks_add",
            "Create a local task. Dates require explicit offsets; reminders queue events, not delivery.",
            Tier.DRAFT,
            _schema(_TEXT_FIELDS, ["title"]),
            service.add,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "tasks_update",
            "Change a local task at its exact revision, or mark it completed/cancelled/open. "
            "This does not perform the task or withdraw an already queued reminder.",
            Tier.DRAFT,
            _schema(
                {
                    **_TEXT_FIELDS,
                    "id": _ID,
                    "revision": _ID,
                    "status": {"type": "string", "enum": ["open", "completed", "cancelled"]},
                },
                ["id", "revision"],
            ),
            service.update,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "tasks_delete",
            "Permanently remove a local task at its exact revision; asks for approval by default. "
            "Audit, event, and backup copies are not erased.",
            Tier.ACT,
            _schema({"id": _ID, "revision": _ID}, ["id", "revision"]),
            service.delete,
            summarize=lambda args: f"Delete local task {args['id']} at revision {args['revision']}",
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    try:
        load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    return []
