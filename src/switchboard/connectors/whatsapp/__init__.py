"""Read-only WhatsApp personal exports; offline snapshots, never a live account mirror."""

from __future__ import annotations

from switchboard.connectors._archive import archive_migrations, private_import_dir
from switchboard.connectors.whatsapp.service import WhatsAppArchive, build_tools
from switchboard.connectors.whatsapp.settings import NAME, load_settings
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.tools import ToolRegistry

__all__ = ["MIGRATIONS", "NAME", "doctor", "register"]

MIGRATIONS = archive_migrations(NAME)


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Register local import and read tools without reading files, scheduling jobs, or accessing accounts."""
    service = WhatsAppArchive(ctx, load_settings(ctx.connector_config(NAME)))
    for tool in build_tools(service):
        registry.add(tool)


def doctor(ctx: AppContext) -> list[str]:
    """Check configuration and import-folder permissions without reading private exports."""
    try:
        settings = load_settings(ctx.connector_config(NAME))
        private_import_dir(settings.import_dir)
    except (ConfigError, ToolError) as exc:
        return [str(exc)]
    return []
