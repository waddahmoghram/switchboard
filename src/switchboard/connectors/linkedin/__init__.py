"""Own LinkedIn exports, local drafts, and optional approval-gated own-profile text posts."""

from __future__ import annotations

from switchboard.connectors._archive import private_import_dir
from switchboard.connectors.linkedin.drafts import MIGRATIONS
from switchboard.connectors.linkedin.service import LinkedInService, build_tools
from switchboard.connectors.linkedin.settings import NAME, load_settings
from switchboard.context import AppContext
from switchboard.errors import ConfigError, SecretError, ToolError
from switchboard.tools import ToolRegistry

__all__ = ["MIGRATIONS", "NAME", "doctor", "register"]


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Register tools offline; no jobs, secret reads, identity lookups, or cross-connector services."""
    load_settings(ctx.connector_config(NAME))
    service = LinkedInService(ctx)
    for tool in build_tools(service):
        registry.add(tool)


def doctor(ctx: AppContext) -> list[str]:
    """Report local configuration and credential availability, never a provider probe or secret value."""
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    if settings.import_dir is not None:
        try:
            private_import_dir(settings.import_dir)
        except ToolError as exc:
            problems.append(str(exc))
    if settings.publishing_enabled:
        try:
            ctx.secret(settings.access_token)
        except SecretError:
            problems.append("the LinkedIn access token could not be resolved")
    return problems
