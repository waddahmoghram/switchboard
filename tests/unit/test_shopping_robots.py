"""Unit tests for robots.txt handling and Retry-After parsing. No real network: FakeHttp only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from switchboard.app import App
from switchboard.connectors.shopping import store
from switchboard.connectors.shopping.prices import run_price_checks
from switchboard.connectors.shopping.robots import (
    is_allowed,
    matches,
    parse_retry_after,
    product_token,
    robots_url,
    rules_for,
)
from switchboard.connectors.shopping.settings import Settings
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
SETTINGS = Settings()
ROBOTS = "https://shop.example.com/robots.txt"
PRODUCT = "https://shop.example.com/products/trail-pack-38l"
OTHER_PRODUCT = "https://shop.example.com/products/camp-stove"
PAGE = (
    '<script type="application/ld+json">'
    '{"@type": "Product", "name": "Trail Pack 38L", "offers": {"price": "60.00", "priceCurrency": "USD"}}'
    "</script>"
)
CONFIG = {"switchboard": {"connectors": ["shopping"]}}


def watch(app: App, url: str = PRODUCT) -> int:
    watch_id, _ = store.add_watch(
        app.ctx.db, url=url, target=Decimal("50"), currency="USD", note=None, now=NOW
    )
    return watch_id


def urls(fake_http: FakeHttp) -> list[str]:
    return [request.url for request in fake_http.requests]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", NOW + timedelta(seconds=120)),
        (" 86400 ", NOW + timedelta(days=1)),
        ("Fri, 13 Nov 2026 12:00:00 GMT", datetime(2026, 11, 13, 12, 0, tzinfo=UTC)),
        ("Wed, 11 Nov 2026 11:00:00 GMT", NOW),  # a date in the past means "now"
        ("99999999999", NOW + timedelta(days=30)),  # capped
        ("soon", None),
        ("-1", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after(value: str | None, expected: datetime | None) -> None:
    assert parse_retry_after(value, NOW) == expected


def test_robots_url_keeps_scheme_and_host_only() -> None:
    assert (
        robots_url("https://shop.example.com:8443/p/1?color=red#reviews")
        == "https://shop.example.com:8443/robots.txt"
    )


def test_disallowed_page_is_never_fetched(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nDisallow: /products/\n")
    results = run_price_checks(app.ctx, SETTINGS, NOW)
    assert [result.outcome for result in results] == ["robots_disallowed"]
    assert urls(fake_http) == [ROBOTS]
    assert results[0].next_allowed_at == "2026-11-11T18:00:00+00:00"


def test_group_for_switchboard_user_agent_is_honored(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, body="User-agent: switchboard\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)] == ["robots_disallowed"]
    assert urls(fake_http) == [ROBOTS]


def test_allowed_page_is_fetched_with_the_robots_file_cached(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    watch(app)
    watch(app, OTHER_PRODUCT)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nDisallow: /cart/\n", repeat=True)
    fake_http.add("GET", PRODUCT, body=PAGE, repeat=True)
    fake_http.add("GET", OTHER_PRODUCT, body=PAGE, repeat=True)

    first = run_price_checks(app.ctx, SETTINGS, NOW)
    assert [result.outcome for result in first] == ["price", "price"]
    assert urls(fake_http) == [ROBOTS, PRODUCT, OTHER_PRODUCT]

    run_price_checks(app.ctx, SETTINGS, NOW + timedelta(hours=7))  # due again; robots.txt still fresh
    assert urls(fake_http).count(ROBOTS) == 1

    run_price_checks(app.ctx, SETTINGS, NOW + timedelta(hours=25))  # older than 24 hours: ask again
    assert urls(fake_http).count(ROBOTS) == 2
    assert urls(fake_http).count(PRODUCT) == 3


def test_missing_robots_file_allows_fetching(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, status=404)
    fake_http.add("GET", PRODUCT, body=PAGE)
    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)] == ["price"]


def test_forbidden_robots_file_means_disallow_everything(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, status=403)
    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)] == ["robots_disallowed"]
    assert urls(fake_http) == [ROBOTS]


def test_unreachable_robots_file_blocks_the_whole_site_for_a_while(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    watch(app)
    watch(app, OTHER_PRODUCT)
    fake_http.add("GET", ROBOTS, status=500)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, body=PAGE)
    fake_http.add("GET", OTHER_PRODUCT, body=PAGE)

    outcomes = [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)]
    assert outcomes == ["robots_unreachable", "robots_unreachable"]
    assert urls(fake_http) == [ROBOTS]  # asked once for the site, not once per watch

    later = NOW + timedelta(hours=6)
    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, later)] == ["price", "price"]
    assert urls(fake_http) == [ROBOTS, ROBOTS, PRODUCT, OTHER_PRODUCT]


def test_rate_limited_robots_file_honors_retry_after(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, status=503, headers={"Retry-After": "172800"})
    (result,) = run_price_checks(app.ctx, SETTINGS, NOW)
    assert result.outcome == "rate_limited"
    assert result.next_allowed_at == "2026-11-13T12:00:00+00:00"
    assert urls(fake_http) == [ROBOTS]


def test_a_rate_limited_page_pauses_every_watch_on_the_site(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    watch(app)
    watch(app, OTHER_PRODUCT)
    fake_http.add("GET", ROBOTS, body="User-agent: *\nAllow: /\n")
    fake_http.add("GET", PRODUCT, status=429, headers={"Retry-After": "86400"})

    results = run_price_checks(app.ctx, SETTINGS, NOW)

    assert [(result.outcome, result.next_allowed_at) for result in results] == [
        ("rate_limited", "2026-11-12T12:00:00+00:00"),
        ("rate_limited", "2026-11-12T12:00:00+00:00"),
    ]
    assert urls(fake_http) == [ROBOTS, PRODUCT]


def test_robots_redirects_are_followed_one_vetted_hop_at_a_time(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    watch(app)
    moved = "https://www.shop.example.com/robots.txt"
    fake_http.add("GET", ROBOTS, status=301, headers={"Location": moved})
    fake_http.add("GET", moved, body="User-agent: *\nDisallow: /\n")

    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)] == ["robots_disallowed"]
    assert urls(fake_http) == [ROBOTS, moved]
    assert all(request.public_only and not request.follow_redirects for request in fake_http.requests)


def test_robots_redirect_to_an_internal_address_is_not_followed(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(CONFIG)
    watch(app)
    fake_http.add("GET", ROBOTS, status=302, headers={"Location": "https://127.0.0.1:8443/robots.txt"})

    assert [result.outcome for result in run_price_checks(app.ctx, SETTINGS, NOW)] == ["robots_unreachable"]
    assert urls(fake_http) == [ROBOTS]


@pytest.mark.parametrize(
    ("robots", "path", "allowed"),
    [
        # Python 3.12's robotparser read "*" literally and fetched these pages.
        ("User-agent: *\nDisallow: /products/*\n", "/products/trail-pack", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/guides/fit.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "/guides/fit.pdf?page=2", True),
        # The longest match wins, wherever it appears; Allow wins a tie.
        ("User-agent: *\nDisallow: /p\nAllow: /products/\n", "/products/a", True),
        ("User-agent: *\nAllow: /products/\nDisallow: /products/*/reviews\n", "/products/a/reviews", False),
        ("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a", True),
        ("User-agent: *\nDisallow: /\n", "/robots.txt", True),
        # Rules and URLs are compared after the same percent-encoding normalization.
        ("User-agent: *\nDisallow: /caf%C3%A9\n", "/caf\u00e9", False),
        ("User-agent: *\nDisallow: /%7euser\n", "/~user/page", False),
        ("User-agent: *\nDisallow: /search # no crawling\n", "/search?q=1", False),
        # Group selection: our product token (any case, any version), else "*", else nothing applies.
        ("User-agent: Switchboard/2.0\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "/x", False),
        ("User-agent: switchboard\nUser-agent: other\nDisallow: /cart\n", "/cart", False),
        ("User-agent: switchboard\nDisallow: /a\n\nUser-agent: switchboard\nDisallow: /b\n", "/b", False),
        ("User-agent: other\nDisallow: /\n", "/x", True),
        ("Disallow: /\nUser-agent: *\nAllow: /\n", "/x", True),
        ("User-agent: *\nDisallow:\n", "/x", True),
    ],
)
def test_rules_follow_rfc_9309(robots: str, path: str, allowed: bool) -> None:
    rules = rules_for(robots, product_token("switchboard/0.1 (self-hosted)"))
    assert is_allowed(rules, "https://shop.example.com" + path) is allowed


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/fish", "/fish.html", True),
        ("/fish$", "/fish.html", False),
        ("/fish*.php", "/fishheads/catfish.php?parameters", True),
        ("/*.php$", "/filename.php", True),
        ("/*.php$", "/filename.php?parameters", False),
        ("/*.php$", "/filename.php/", False),
        ("/fish*", "/Fish.asp", False),
        ("*", "/anything", True),
        ("/a*b*c$", "/axxbyyc", True),
        ("/a*b*c$", "/axxbyycd", False),
        ("/a*bc$", "/abcbc", True),
    ],
)
def test_wildcards_and_end_anchors(pattern: str, path: str, expected: bool) -> None:
    assert matches(pattern, path) is expected


def test_a_hostile_pattern_is_decided_in_one_pass() -> None:
    assert matches("/" + "*a" * 1000 + "*b", "/" + "a" * 2000) is False


@pytest.mark.parametrize(
    ("agent", "token"),
    [("switchboard/0.1 (x)", "switchboard"), ("Switchboard", "switchboard"), ("*", "*"), ("", "")],
)
def test_product_tokens(agent: str, token: str) -> None:
    assert product_token(agent) == token
