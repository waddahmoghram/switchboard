"""Contacts connector: CardDAV address books and local .vcf files, cached for quick lookups.

Assistants can search names, organizations, email addresses, and phone numbers; the connector never
changes an address book. See ``docs/connectors/contacts.md``.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.connectors.contacts.service import ContactsService, build_tools
from switchboard.connectors.contacts.settings import NAME, load_settings
from switchboard.connectors.contacts.store import MIGRATIONS, CardStore
from switchboard.context import AppContext
from switchboard.errors import ConfigError, SecretError
from switchboard.tools import ToolRegistry

__all__ = ["MIGRATIONS", "NAME", "doctor", "register"]


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Check settings, add tools, and schedule ``contacts.refresh``. No network I/O, no secrets read."""
    service = ContactsService(ctx, load_settings(ctx.connector_config(NAME)))
    for tool in build_tools(service):
        registry.add(tool)
    ctx.scheduler.add(f"{NAME}.refresh", service.settings.refresh_seconds, service.run_job)


def doctor(ctx: AppContext) -> list[str]:
    """Settings, secret references, files, and the last refresh error of each source. Never prints secrets."""
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    if not settings.sources:
        problems.append("no sources are configured under [connectors.contacts.sources]")
    store = CardStore(ctx.db, NAME)
    for source in settings.sources.values():
        label = f"source {source.name!r}"
        if source.type == "carddav":
            try:
                ctx.secret(source.password)
            except SecretError as exc:
                problems.append(f"{label}: password: {exc}")
        elif not Path(source.path).expanduser().is_file():
            problems.append(f"{label}: path does not point to a file")
        error = store.state(source.name).get("error")
        if error:
            problems.append(f"{label}: last refresh failed: {error}")
    return problems
