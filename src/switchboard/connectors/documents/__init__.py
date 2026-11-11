"""Allowlisted live local notes: no imports, index, migrations, background work, or write tools."""

from __future__ import annotations

from switchboard.connectors.documents.service import Documents, check_collection
from switchboard.connectors.documents.settings import NAME, load_settings
from switchboard.connectors.documents.tools import build_tools
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.tools import ToolRegistry

__all__ = ["NAME", "doctor", "register"]


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Validate settings and register tools without checking, listing, or reading a configured folder."""
    load_settings(ctx.connector_config(NAME))
    for tool in build_tools(Documents(ctx)):
        registry.add(tool)


def doctor(ctx: AppContext) -> list[str]:
    """Inspect root permissions on explicit request; do not enumerate or read notes."""
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    for collection in settings.collections:
        try:
            check_collection(collection, settings)
        except ToolError as exc:
            problems.append(f"documents collection {collection.name}: {exc}")
    return problems
