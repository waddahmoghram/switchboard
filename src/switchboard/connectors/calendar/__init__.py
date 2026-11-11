"""Calendar connector: secret ICS feeds, CalDAV calendars, and local .ics files.

Assistants can read an agenda, find free time, draft invitations through the mail connector (the
no-OAuth way to add events to Google Calendar), and, after approval, create events on writable CalDAV
calendars or email invitations. See ``docs/connectors/calendar.md``.
"""

from __future__ import annotations

from pathlib import Path

from switchboard.connectors.calendar.service import CalendarService
from switchboard.connectors.calendar.settings import NAME, Source, load_settings
from switchboard.connectors.calendar.store import MIGRATIONS, EventStore
from switchboard.connectors.calendar.tools import build_tools
from switchboard.context import AppContext
from switchboard.errors import ConfigError, SecretError
from switchboard.tools import ToolRegistry

__all__ = ["MIGRATIONS", "NAME", "doctor", "register"]


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Check settings, add tools, and schedule ``calendar.refresh``. No network I/O, no secrets read."""
    service = CalendarService(ctx, load_settings(ctx.connector_config(NAME)))
    for tool in build_tools(service):
        registry.add(tool)
    ctx.scheduler.add(f"{NAME}.refresh", service.settings.refresh_seconds, service.run_job)


def _source_problems(ctx: AppContext, source: Source) -> list[str]:
    problems: list[str] = []
    references = {"ics_url": [("url", source.url)], "caldav": [("password", source.password)]}
    for label, reference in references.get(source.type, []):
        try:
            ctx.secret(reference)
        except SecretError as exc:
            problems.append(f"{label}: {exc}")
    if source.type == "ics_file" and not Path(source.path).expanduser().is_file():
        problems.append("path does not point to a file")
    return problems


def doctor(ctx: AppContext) -> list[str]:
    """Settings, secret references, files, and the last refresh error of each source. Never prints secrets."""
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    problems: list[str] = []
    if not settings.sources:
        problems.append("no sources are configured under [connectors.calendar.sources]")
    if settings.upcoming_minutes * 60 < settings.refresh_seconds:
        problems.append(
            "upcoming_minutes is shorter than refresh_interval_seconds, "
            "so some events would never be announced"
        )
    store = EventStore(ctx.db, NAME)
    for source in settings.sources.values():
        found = _source_problems(ctx, source)
        error = store.state(source.name).get("error")
        if error:
            found.append(f"last refresh failed: {error}")
        problems += [f"source {source.name!r}: {problem}" for problem in found]
    return problems
