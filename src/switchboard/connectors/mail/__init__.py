"""Mail connector: search, read, and draft mail over IMAP; send over SMTP with approval.

Setup per provider, the tools, events, services, and design tradeoffs are in docs/connectors/mail.md.
"""

from __future__ import annotations

from switchboard.connectors.mail.accounts import parse_settings
from switchboard.connectors.mail.checks import doctor
from switchboard.connectors.mail.cli import add_cli
from switchboard.connectors.mail.service import NAME, MailService
from switchboard.connectors.mail.tools import build_tools
from switchboard.context import AppContext
from switchboard.tools import ToolRegistry

__all__ = ["NAME", "add_cli", "doctor", "register"]


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Add the tools, provide ``mail.drafts`` and ``mail.sender``, and schedule ``mail.poll``.

    Only parses configuration: secrets are resolved and servers contacted when a tool or job runs.
    The poll job is scheduled only when some account watches a mailbox.
    """
    settings = parse_settings(ctx.connector_config(NAME))
    service = MailService(ctx, settings)
    for tool in build_tools(service):
        registry.add(tool)
    ctx.provide("mail.drafts", service)
    ctx.provide("mail.sender", service)
    if any(account.watch for account in settings.accounts.values()):
        ctx.scheduler.add("mail.poll", settings.poll_interval_seconds, service.poll)
