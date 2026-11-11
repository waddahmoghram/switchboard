"""Read-only, explicitly named server, backup-report, and disk-capacity monitoring."""

from __future__ import annotations

from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ConfigError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry

from .service import MonitoringService
from .settings import NAME_PATTERN, load_settings
from .store import MIGRATIONS as MIGRATIONS

NAME = "monitoring"


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Register tools, a service, and a future polling job, without running any checks."""
    settings = load_settings(ctx.connector_config(NAME))
    service = MonitoringService(ctx, settings)
    ctx.provide("monitoring", service)
    ctx.scheduler.add("monitoring.poll", settings.poll_interval_seconds, service.poll_job)
    name = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": f"^{NAME_PATTERN}$",
        "enum": sorted(service.checks),
    }
    for tool_name, description, required, handler in (
        (
            "monitoring_status",
            "Read cached named health, backup-report, and disk-capacity snapshots. "
            "No new measurements; stale data is not current health.",
            False,
            service.status,
        ),
        (
            "monitoring_check",
            "Run one configured read-only check and cache its result. Accepts only a configured name, "
            "never an address, path, or command. Does not perform backups, restores, or remote writes.",
            True,
            service.check,
        ),
    ):
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"name": name},
            "additionalProperties": False,
        }
        if required:
            schema["required"] = ["name"]
        registry.add(Tool(tool_name, description, Tier.READ, schema, handler, connector=NAME))


def doctor(ctx: AppContext) -> list[str]:
    """Check configuration only, not credentials, endpoints, reports, capacity, or restore integrity."""
    try:
        load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    return []
