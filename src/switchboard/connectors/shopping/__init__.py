"""Shopping: order and shipment tracking from mail, and polite price watches. It never buys anything.

* Subscribes to ``mail.received``; emits ``shopping.order``, ``shopping.shipment``, and
  ``shopping.price_drop``.
* Schedules ``shopping.prices``, which honors robots.txt, Retry-After, and a per-URL minimum interval.
* Uses no services and provides none.

Purchasing is deliberately not implemented. The assistant tells the user that a watched item reached
the target and gives the link; the user decides and buys. See ``docs/connectors/shopping.md``.
"""

from __future__ import annotations

from switchboard.connectors.shopping.orders import handle_mail
from switchboard.connectors.shopping.prices import run_price_checks
from switchboard.connectors.shopping.settings import NAME, parse_settings
from switchboard.connectors.shopping.store import MIGRATIONS
from switchboard.connectors.shopping.toolset import build_tools
from switchboard.context import AppContext
from switchboard.events import Event
from switchboard.tools import ToolRegistry
from switchboard.util import now

__all__ = ["MIGRATIONS", "NAME", "doctor", "register"]

PRICE_JOB_DELAY_SECONDS = 120


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    """Subscribe to mail, schedule the price job, and add tools. Offline and fast.

    Configuration problems never stop startup here; ``doctor`` reports them.
    """
    settings, _problems = parse_settings(ctx.connector_config(NAME))

    def on_mail(event: Event) -> None:
        handle_mail(ctx.db, ctx.events, settings, event)

    def check_prices() -> None:
        run_price_checks(ctx, settings, now())

    ctx.events.subscribe("mail.received", "shopping.orders", on_mail)
    ctx.scheduler.add(
        "shopping.prices", settings.price_check_interval_seconds, check_prices, delay=PRICE_JOB_DELAY_SECONDS
    )
    for tool in build_tools(ctx, settings):
        registry.add(tool)


def doctor(ctx: AppContext) -> list[str]:
    """Configuration problems: regexes that do not compile, malformed allowed_domains, bad intervals."""
    return parse_settings(ctx.connector_config(NAME))[1]
