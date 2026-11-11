"""Personal finance from files you download and alert emails you already receive; no aggregator.

* Imports OFX, QFX, and CSV statement files from an inbox directory you own.
* Turns bank alert emails (``mail.received`` events) into pending transactions.
* Keeps a local SQLite ledger with masked account numbers, monthly summaries, recurring-charge
  detection, and budgets.
* Has no tools that move money.

See ``docs/connectors/finance.md``.
"""

from __future__ import annotations

import logging

from switchboard.connectors.finance.cli import add_cli
from switchboard.connectors.finance.ledger import MIGRATIONS
from switchboard.connectors.finance.models import NAME
from switchboard.connectors.finance.runtime import FinanceRuntime, doctor_problems
from switchboard.connectors.finance.tools import register_tools
from switchboard.context import AppContext
from switchboard.tools import ToolRegistry

__all__ = ["MIGRATIONS", "NAME", "add_cli", "doctor", "register"]

log = logging.getLogger(__name__)


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Add the finance tools and the bank-alert subscriber. Reads configuration only; no I/O."""
    runtime = FinanceRuntime(ctx)
    for problem in runtime.settings.problems:
        log.warning("finance configuration: %s", problem)
    register_tools(runtime, registry)
    ctx.events.subscribe("mail.received", "finance.alerts", runtime.on_mail_event)


def doctor(ctx: AppContext) -> list[str]:
    """Problems with the finance configuration and the import inbox."""
    return doctor_problems(ctx)
