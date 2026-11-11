"""Unit tests for the price-watch job: drops, dedupe, Retry-After backoff, and safe failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from switchboard.app import App
from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.prices import is_price_drop, run_price_checks
from switchboard.connectors.shopping.settings import Settings, parse_settings
from switchboard.errors import HttpError
from switchboard.httpclient import HttpResponse
from tests.conftest import AppFactory
from tests.fakes import FakeHttp, RecordedRequest

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
SETTINGS = Settings()
ROBOTS = "https://shop.example.com/robots.txt"
PRODUCT = "https://shop.example.com/products/trail-pack-38l"
CONFIG = {"switchboard": {"connectors": ["shopping"]}}


def product_page(price: str, currency: str = "USD") -> str:
    return (
        '<script type="application/ld+json">{"@type": "Product", "name": "Trail Pack 38L", '
        f'"offers": {{"price": "{price}", "priceCurrency": "{currency}"}}}}</script>'
    )


def add_watch(app: App, target: str = "50") -> int:
    watch_id, _ = store.add_watch(
        app.ctx.db, url=PRODUCT, target=Decimal(target), currency="USD", note=None, now=NOW
    )
    return watch_id


def price_drops(app: App) -> list[dict[str, object]]:
    events = [event for event in app.ctx.events.recent(100) if event.type == "shopping.price_drop"]
    return [event.payload for event in reversed(events)]


@pytest.mark.parametrize(
    ("price", "target", "previous", "expected"),
    [
        ("45", "50", None, True),  # a first observation at or below the target counts
        ("50", "50", None, True),
        ("45", "50", "60", True),
        ("45", "50", "45", False),  # unchanged
        ("45", "50", "40", False),  # went up
        ("55", "50", "60", False),  # lower, but above the target
    ],
)
def test_is_price_drop(price: str, target: str, previous: str | None, expected: bool) -> None:
    before = Decimal(previous) if previous is not None else None
    assert is_price_drop(Decimal(price), Decimal(target), before) is expected


def test_price_drop_is_emitted_once_per_watch_and_price(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nDisallow: /cart/\n", repeat=True)
    for price in ("45.00", "45.00", "40.00", "60.00", "40.00"):
        fake_http.add("GET", PRODUCT, body=product_page(price))

    alerts = []
    for hours in (0, 7, 14, 21, 28):
        (result,) = run_price_checks(app.ctx, SETTINGS, NOW + timedelta(hours=hours))
        assert result.outcome == "price"
        alerts.append(result.alerted)

    # 45 (first, under target), 45 (unchanged), 40 (lower), 60 (above target), 40 (already announced)
    assert alerts == [True, False, True, False, False]
    assert price_drops(app) == [
        {
            "watch_id": 1,
            "url": PRODUCT,
            "title": "Trail Pack 38L",
            "price": "45.00",
            "currency": "USD",
            "target": "50.00",
            "previous": None,
        },
        {
            "watch_id": 1,
            "url": PRODUCT,
            "title": "Trail Pack 38L",
            "price": "40.00",
            "currency": "USD",
            "target": "50.00",
            "previous": "45.00",
        },
    ]
    listing = store.list_watches(app.ctx.db, include_inactive=False, limit=10)
    assert (listing[0]["last_price"], listing[0]["title"]) == ("40.00", "Trail Pack 38L")


def test_too_many_requests_with_seconds_sets_next_allowed_at(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=429, headers={"Retry-After": "86400"})

    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert result.outcome == "rate_limited"
    assert result.next_allowed_at == "2026-11-12T12:00:00+00:00"

    # Twelve hours later the regular interval has passed, but Retry-After has not: nothing is fetched.
    assert run_price_checks(app.ctx, SETTINGS, NOW + timedelta(hours=12)) == []
    assert len(fake_http.requests) == 2


def test_unavailable_with_http_date_sets_next_allowed_at(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=503, headers={"Retry-After": "Fri, 13 Nov 2026 12:00:00 GMT"})
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert (result.outcome, result.next_allowed_at) == ("rate_limited", "2026-11-13T12:00:00+00:00")


def test_short_retry_after_never_beats_the_minimum_interval(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=429, headers={"Retry-After": "5"})
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert result.next_allowed_at == "2026-11-11T18:00:00+00:00"


def test_page_in_another_currency_never_alerts(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, status=404)
    fake_http.add("GET", PRODUCT, body=product_page("10.00", "EUR"))
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert (result.outcome, result.price, result.currency, result.alerted) == (
        "price_other_currency",
        "10.00",
        "EUR",
        False,
    )
    assert price_drops(app) == []


def test_domain_outside_allowed_domains_is_not_fetched(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    settings, _ = parse_settings({"allowed_domains": ["outfitters.example.org"]})
    (result,) = run_price_checks(app.ctx, settings, NOW)
    assert result.outcome == "domain_not_allowed"
    assert fake_http.requests == []


def test_page_without_structured_price_is_recorded_as_no_price(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, status=404)
    fake_http.add("GET", PRODUCT, body="<html><body><p>Only $19.99 today!</p></body></html>")
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert (result.outcome, result.price) == ("no_price", None)


def test_network_failure_is_contained(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)

    def refuse(_request: RecordedRequest) -> HttpResponse:
        raise HttpError("GET shop.example.com: connection refused")

    fake_http.add("GET", ROBOTS, status=404)
    fake_http.add_handler("GET", PRODUCT, refuse)
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert result.outcome == "fetch_failed"
    assert result.next_allowed_at == "2026-11-11T18:00:00+00:00"


def test_removed_watch_is_not_checked(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch_id = add_watch(app)
    assert store.deactivate_watch(app.ctx.db, watch_id, NOW)
    assert run_price_checks(app.ctx, SETTINGS, NOW) == []
    assert fake_http.requests == []


def test_page_redirects_are_followed_one_vetted_hop_at_a_time(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    moved = "https://shop.example.com/products/trail-pack-38l-v2"
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=301, headers={"Location": "/products/trail-pack-38l-v2"})
    fake_http.add("GET", moved, body=product_page("40.00"))

    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)

    assert (result.outcome, result.price) == ("price", "40.00")
    assert [request.url for request in fake_http.requests] == [ROBOTS, PRODUCT, moved]
    assert all(request.public_only and not request.follow_redirects for request in fake_http.requests)


@pytest.mark.parametrize(
    "location",
    [
        "https://10.0.0.5/admin",
        "https://localhost:8443/",
        "https://printer.lan/status",
        "http://shop.example.com/products/trail-pack-38l",
        "https://user:secret@shop.example.com/",
    ],
)
def test_redirects_to_internal_or_unsafe_targets_are_never_fetched(
    make_app: AppFactory, fake_http: FakeHttp, location: str
) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=302, headers={"Location": location})

    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)

    assert result.outcome == "fetch_failed"
    assert [request.url for request in fake_http.requests] == [ROBOTS, PRODUCT]


def test_redirect_outside_allowed_domains_is_refused(make_app: AppFactory, fake_http: FakeHttp) -> None:
    settings, _ = parse_settings({"allowed_domains": ["shop.example.com"]})
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=302, headers={"Location": "https://tracker.example.net/p"})

    (result,) = run_price_checks(app.ctx, settings, NOW)

    assert result.outcome == "fetch_failed"
    assert [request.url for request in fake_http.requests] == [ROBOTS, PRODUCT]


def test_endless_redirects_stop_after_five_hops(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    add_watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=302, headers={"Location": PRODUCT}, repeat=True)

    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)

    assert result.outcome == "fetch_failed"
    assert [request.url for request in fake_http.requests].count(PRODUCT) == 6
