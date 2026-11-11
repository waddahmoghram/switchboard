"""The price-watch job: polite fetches, price history, and ``shopping.price_drop`` events.

For each due watch: check ``allowed_domains``, reserve the watch (so two processes never fetch the
same page), ask robots.txt, fetch the page (capped at 2 MB), extract the price, store it, and emit
``shopping.price_drop`` when the price is at or below the target and lower than the previous
observation. One event per watch per price: the dedupe key includes both.

Nothing here buys anything. The event carries the link; a person decides and checks out.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.fetch import get_vetted
from switchboard.connectors.shopping.money import price_text
from switchboard.connectors.shopping.pages import PriceFound, extract_price
from switchboard.connectors.shopping.robots import parse_retry_after, pause_site, robots_verdict
from switchboard.connectors.shopping.settings import NAME, Settings
from switchboard.context import AppContext
from switchboard.errors import HttpError
from switchboard.util import iso

log = logging.getLogger(__name__)

MAX_PAGE_BYTES = 2_000_000
MAX_CHECKS_PER_RUN = 25
_PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"}


@dataclass(frozen=True)
class CheckResult:
    watch_id: int
    url: str
    outcome: str
    next_allowed_at: str
    price: str | None = None
    currency: str | None = None
    alerted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_price_drop(price: Decimal, target: Decimal, previous: Decimal | None) -> bool:
    """At or below the target, and lower than the last observation (a first observation counts)."""
    return price <= target and (previous is None or price < previous)


def run_price_checks(ctx: AppContext, settings: Settings, now: datetime) -> list[CheckResult]:
    """Check every due watch once. A failure on one page never stops the others."""
    results: list[CheckResult] = []
    for watch in store.due_watches(ctx.db, now, MAX_CHECKS_PER_RUN):
        try:
            result = check_watch(ctx, settings, watch, now)
        except Exception:
            # Log the id, not the URL: product URLs can carry session or affiliate tokens.
            log.exception("price check for watch %s failed", watch.id)
            result = _finish(ctx, watch, now, _next_regular(settings, now), "error")
        if result is not None:
            results.append(result)
    return results


def check_watch(ctx: AppContext, settings: Settings, watch: store.Watch, now: datetime) -> CheckResult | None:
    """One polite check; None when another process claimed the watch first."""
    regular = _next_regular(settings, now)
    if not settings.allows_host(urlsplit(watch.url).hostname or ""):
        return _finish(ctx, watch, now, regular, "domain_not_allowed")
    if not store.claim_watch(ctx.db, watch, now=now, until=regular):
        return None
    verdict = robots_verdict(ctx.http, ctx.db, watch.url, now, settings)
    if not verdict.allowed:
        return _finish(ctx, watch, now, _later(regular, verdict.retry_at), verdict.outcome)
    try:
        response = get_vetted(ctx.http, watch.url, settings, headers=_PAGE_HEADERS, max_bytes=MAX_PAGE_BYTES)
    except HttpError:
        return _finish(ctx, watch, now, regular, "fetch_failed")
    if response.status in (429, 503):
        retry_at = parse_retry_after(response.headers.get("retry-after"), now)
        until = pause_site(ctx.db, watch.url, now, retry_at)  # the other watches on this site wait too
        return _finish(ctx, watch, now, _later(regular, until), "rate_limited")
    if not response.ok:
        return _finish(ctx, watch, now, regular, f"http_{response.status}")
    found = extract_price(response.text())
    if found is None:
        return _finish(ctx, watch, now, regular, "no_price")
    return _record(ctx, watch, found, now, regular)


def _record(
    ctx: AppContext, watch: store.Watch, found: PriceFound, now: datetime, regular: datetime
) -> CheckResult:
    """Store the observation and alert when it is a drop. A page in another currency never alerts."""
    previous = store.save_observation(ctx.db, watch.id, found, now=now, next_allowed_at=regular)
    currency = found.currency or watch.currency
    if currency != watch.currency:
        outcome, alerted = "price_other_currency", False
    else:
        outcome = "price"
        alerted = is_price_drop(found.price, watch.target_price, previous) and _emit_drop(
            ctx, watch, found, previous
        )
    return CheckResult(watch.id, watch.url, outcome, iso(regular), price_text(found.price), currency, alerted)


def _emit_drop(ctx: AppContext, watch: store.Watch, found: PriceFound, previous: Decimal | None) -> bool:
    """Queue ``shopping.price_drop``; False when this watch already alerted at this price."""
    price = price_text(found.price)
    payload = {
        "watch_id": watch.id,
        "url": watch.url,
        "title": watch.title or found.title,
        "price": price,
        "currency": watch.currency,
        "target": price_text(watch.target_price),
        "previous": price_text(previous) if previous is not None else None,
    }
    dedupe_key = f"shopping:price_drop:{watch.id}:{price}"
    return ctx.events.emit(NAME, "shopping.price_drop", payload, dedupe_key=dedupe_key) is not None


def _finish(ctx: AppContext, watch: store.Watch, now: datetime, until: datetime, outcome: str) -> CheckResult:
    store.finish_check(ctx.db, watch.id, now=now, next_allowed_at=until, result=outcome)
    return CheckResult(watch.id, watch.url, outcome, iso(until))


def _next_regular(settings: Settings, now: datetime) -> datetime:
    return now + timedelta(seconds=settings.min_fetch_interval_seconds)


def _later(regular: datetime, retry_at: datetime | None) -> datetime:
    return regular if retry_at is None else max(regular, retry_at)
