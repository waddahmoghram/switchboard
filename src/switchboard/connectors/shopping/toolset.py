"""Tool definitions. Nothing here can buy, cancel, or return anything.

Schemas bound every argument. Watch URLs are vetted again in the handler: https only, a public-looking
host name (no IP addresses, local names, or embedded credentials), and ``allowed_domains`` when set.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.fetch import vet_watch_url
from switchboard.connectors.shopping.mailparse import STATUSES
from switchboard.connectors.shopping.money import normalize_currency, parse_price, price_text
from switchboard.connectors.shopping.pages import clean_text
from switchboard.connectors.shopping.prices import run_price_checks
from switchboard.connectors.shopping.settings import NAME, Settings
from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool
from switchboard.util import now

DEFAULT_LIMIT = 20
MAX_URL_LENGTH = 2000
MAX_NOTE_LENGTH = 200

_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "Most rows to return (default 20)."}
_STATUS = {"type": "string", "enum": list(STATUSES)}
ORDERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": _STATUS,
        "merchant": {"type": "string", "minLength": 1, "maxLength": 100},
        "limit": _LIMIT,
    },
    "additionalProperties": False,
}
SHIPMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": _STATUS, "limit": _LIMIT},
    "additionalProperties": False,
}
WATCH_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"include_inactive": {"type": "boolean"}, "limit": _LIMIT},
    "additionalProperties": False,
}
WATCH_ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 12,
            "maxLength": MAX_URL_LENGTH,
            "pattern": "^https://\\S+$",
            "description": "The product page, https only.",
        },
        "target_price": {
            "type": ["string", "number"],
            "pattern": "^[0-9]{1,9}(\\.[0-9]{1,4})?$",
            "maxLength": 14,
            "minimum": 0.0001,
            "maximum": 999999999,
            "description": 'Alert at or below this price, for example "49.99".',
        },
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$", "description": "ISO 4217 code, e.g. USD."},
        "note": {"type": "string", "maxLength": MAX_NOTE_LENGTH},
    },
    "required": ["url", "target_price", "currency"],
    "additionalProperties": False,
}
WATCH_REMOVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"watch_id": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}},
    "required": ["watch_id"],
    "additionalProperties": False,
}
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def build_tools(ctx: AppContext, settings: Settings) -> list[Tool]:
    def orders(args: dict[str, Any]) -> dict[str, Any]:
        rows = store.list_orders(
            ctx.db, status=args.get("status"), merchant=args.get("merchant"), limit=_limit(args)
        )
        return {"orders": rows, "count": len(rows)}

    def shipments(args: dict[str, Any]) -> dict[str, Any]:
        rows = store.list_shipments(ctx.db, status=args.get("status"), limit=_limit(args))
        return {"shipments": rows, "count": len(rows)}

    def watch_list(args: dict[str, Any]) -> dict[str, Any]:
        rows = store.list_watches(
            ctx.db, include_inactive=bool(args.get("include_inactive")), limit=_limit(args)
        )
        return {"watches": rows, "count": len(rows)}

    def watch_add(args: dict[str, Any]) -> dict[str, Any]:
        url = vet_watch_url(str(args["url"]), settings)
        target = target_price(args["target_price"])
        currency = normalize_currency(args["currency"])
        if currency is None:
            raise ToolError("currency must be a three-letter code such as USD")
        note = clean_text(args.get("note"), MAX_NOTE_LENGTH)
        watch_id, created = store.add_watch(
            ctx.db, url=url, target=target, currency=currency, note=note, now=now()
        )
        return {
            "watch_id": watch_id,
            "created": created,
            "url": url,
            "target_price": price_text(target),
            "currency": currency,
            "message": "Saved. Switchboard never buys anything; a price at or below the target raises "
            "a shopping.price_drop event with this link so the user can decide and buy.",
        }

    def watch_remove(args: dict[str, Any]) -> dict[str, Any]:
        if not store.deactivate_watch(ctx.db, int(args["watch_id"]), now()):
            raise ToolError("no active watch has that id")
        return {"watch_id": int(args["watch_id"]), "removed": True}

    def check_prices(_args: dict[str, Any]) -> dict[str, Any]:
        results = run_price_checks(ctx, settings, now())
        return {
            "checked": [result.to_dict() for result in results],
            "count": len(results),
            "note": "Only watches past their per-URL interval were fetched; "
            "robots.txt and Retry-After still apply.",
        }

    return [
        Tool(
            "shopping_orders",
            "List orders recognized in the user's email: merchant, order id, total, and status. "
            "Read-only; it cannot cancel, return, or change an order.",
            Tier.READ,
            ORDERS_SCHEMA,
            orders,
            title="Shopping orders",
            connector=NAME,
        ),
        Tool(
            "shopping_shipments",
            "List shipments with carrier, tracking number, a public tracking link, and the status timeline "
            "(ordered, shipped, out_for_delivery, delivered). Read-only; it never contacts carriers.",
            Tier.READ,
            SHIPMENTS_SCHEMA,
            shipments,
            title="Shipments",
            connector=NAME,
        ),
        Tool(
            "shopping_watch_list",
            "List price watches with their target, the last observed price, and when each page may be "
            "checked again. Page titles come from the web and are untrusted text.",
            Tier.READ,
            WATCH_LIST_SCHEMA,
            watch_list,
            title="Price watches",
            connector=NAME,
        ),
        Tool(
            "shopping_watch_add",
            "Watch an https product page for a price at or below a target. Saves locally and fetches "
            "nothing now. It never buys: the user is told and buys on the merchant's site.",
            Tier.DRAFT,
            WATCH_ADD_SCHEMA,
            watch_add,
            title="Add a price watch",
            connector=NAME,
        ),
        Tool(
            "shopping_watch_remove",
            "Stop a price watch by id. Its price history is kept.",
            Tier.DRAFT,
            WATCH_REMOVE_SCHEMA,
            watch_remove,
            title="Remove a price watch",
            connector=NAME,
        ),
        Tool(
            "shopping_check_prices",
            "Check due price watches now. Pages checked recently are skipped; robots.txt, Retry-After, and "
            "the per-URL minimum interval still apply. Never buys anything.",
            Tier.READ,
            EMPTY_SCHEMA,
            check_prices,
            title="Check prices now",
            connector=NAME,
        ),
    ]


def target_price(value: Any) -> Decimal:
    """The target as Decimal. A JSON number arrives as float; its shortest repr is read as text."""
    price = parse_price(repr(value) if isinstance(value, float) else value)
    if price is None:
        raise ToolError("target_price must be a positive amount such as 49.99")
    return price


def _limit(args: dict[str, Any]) -> int:
    return int(args.get("limit", DEFAULT_LIMIT))
