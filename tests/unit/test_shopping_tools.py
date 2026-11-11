"""Unit tests for the shopping tools as an assistant sees them, through the policy gate."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard.app import App
from switchboard.policy import Origin, Tier
from switchboard.tools import CallOutcome
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

PRODUCT = "https://shop.example.com/products/trail-pack-38l"
CONFIG = {"switchboard": {"connectors": ["shopping"]}}
SHOPPING_TOOLS = {
    "shopping_orders": Tier.READ,
    "shopping_shipments": Tier.READ,
    "shopping_watch_list": Tier.READ,
    "shopping_watch_add": Tier.DRAFT,
    "shopping_watch_remove": Tier.DRAFT,
    "shopping_check_prices": Tier.READ,
}


def call(app: App, tool: str, args: dict[str, Any] | None = None) -> CallOutcome:
    return app.call(tool, args or {}, Origin.ASSISTANT)


def test_tools_are_registered_with_low_tiers_and_closed_schemas(make_app: AppFactory) -> None:
    app = make_app(CONFIG)
    registered = {tool.name: tool for tool in app.registry.all() if tool.connector == "shopping"}
    assert {name: tool.tier for name, tool in registered.items()} == SHOPPING_TOOLS
    for tool in registered.values():
        assert tool.input_schema["additionalProperties"] is False
    assert app.ctx.scheduler.jobs()[-1].name == "shopping.prices"


def test_watch_add_saves_without_fetching(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    outcome = call(
        app,
        "shopping_watch_add",
        {"url": PRODUCT + "#reviews", "target_price": 49.99, "currency": "USD", "note": "for the\x00 trip"},
    )
    assert outcome.status == "ok", outcome.message
    assert outcome.data["watch_id"] == 1
    assert outcome.data["created"] is True
    assert outcome.data["url"] == PRODUCT  # the fragment is dropped
    assert outcome.data["target_price"] == "49.99"
    assert fake_http.requests == []

    listing = call(app, "shopping_watch_list")
    (watch,) = listing.data["watches"]
    assert (watch["url"], watch["target_price"], watch["currency"], watch["note"]) == (
        PRODUCT,
        "49.99",
        "USD",
        "for the trip",
    )
    assert watch["last_price"] is None


def test_adding_the_same_url_updates_the_existing_watch(make_app: AppFactory) -> None:
    app = make_app(CONFIG)
    first = call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": "1,299.00", "currency": "USD"})
    assert first.status == "invalid"  # the schema wants plain digits for string prices
    first = call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": "1299", "currency": "USD"})
    second = call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": 1100, "currency": "USD"})
    assert (first.data["watch_id"], first.data["created"]) == (1, True)
    assert (second.data["watch_id"], second.data["created"]) == (1, False)
    assert call(app, "shopping_watch_list").data["watches"][0]["target_price"] == "1100.00"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://192.0.2.10/item", "public host name"),
        ("https://nas.local/item", "public host name"),
        ("https://intranet/item", "public host name"),
        ("https://owner@shop.example.com/item", "embedded credentials"),
    ],
)
def test_watch_add_refuses_risky_urls(make_app: AppFactory, url: str, reason: str) -> None:
    app = make_app(CONFIG)
    outcome = call(app, "shopping_watch_add", {"url": url, "target_price": "10", "currency": "USD"})
    assert outcome.status == "error"
    assert reason in outcome.message


@pytest.mark.parametrize(
    "args",
    [
        {"url": "http://shop.example.com/item", "target_price": "10", "currency": "USD"},
        {"url": PRODUCT, "target_price": "10", "currency": "usd"},
        {"url": PRODUCT, "target_price": "-1", "currency": "USD"},
        {"url": PRODUCT, "target_price": 0, "currency": "USD"},
        {"url": PRODUCT, "target_price": "10", "currency": "USD", "buy": True},
        {"url": PRODUCT, "currency": "USD"},
    ],
)
def test_watch_add_schema_rejects_bad_arguments(make_app: AppFactory, args: dict[str, Any]) -> None:
    assert call(make_app(CONFIG), "shopping_watch_add", args).status == "invalid"


def test_allowed_domains_limit_new_watches(make_app: AppFactory) -> None:
    app = make_app(
        {
            "switchboard": {"connectors": ["shopping"]},
            "connectors": {"shopping": {"allowed_domains": ["example.org"]}},
        }
    )
    refused = call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": "10", "currency": "USD"})
    assert refused.status == "error"
    assert "allowed_domains" in refused.message
    allowed = call(
        app,
        "shopping_watch_add",
        {"url": "https://www.example.org/p/1", "target_price": "10", "currency": "USD"},
    )
    assert allowed.status == "ok", allowed.message


def test_watch_remove_keeps_history_and_hides_the_watch(make_app: AppFactory) -> None:
    app = make_app(CONFIG)
    call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": "10", "currency": "USD"})
    assert call(app, "shopping_watch_remove", {"watch_id": 1}).data == {"watch_id": 1, "removed": True}
    assert call(app, "shopping_watch_list").data["watches"] == []
    hidden = call(app, "shopping_watch_list", {"include_inactive": True}).data["watches"]
    assert [(watch["watch_id"], watch["active"]) for watch in hidden] == [(1, False)]
    again = call(app, "shopping_watch_remove", {"watch_id": 1})
    assert (again.status, again.message) == ("error", "no active watch has that id")


def test_check_prices_with_nothing_due_fetches_nothing(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    outcome = call(app, "shopping_check_prices")
    assert outcome.status == "ok"
    assert outcome.data["count"] == 0
    assert fake_http.requests == []


def test_kill_switch_still_allows_reading_but_not_adding(make_app: AppFactory) -> None:
    app = make_app(CONFIG)
    app.policy.kill_switch.touch()
    assert call(app, "shopping_orders").status == "ok"
    added = call(app, "shopping_watch_add", {"url": PRODUCT, "target_price": "10", "currency": "USD"})
    assert added.status == "denied"
