"""Unit tests for the [connectors.shopping] table and the doctor check."""

from __future__ import annotations

from typing import Any

from switchboard.connectors.shopping.settings import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_MIN_FETCH_INTERVAL,
    MIN_CHECK_INTERVAL,
    Settings,
    parse_settings,
)
from tests.conftest import AppFactory

GOOD_RULE = {
    "name": "Example Outfitters",
    "from_contains": "@Orders.Example.com",
    "shipped_subject_regex": r"\bhas shipped\b",
}
BROKEN_RULE = {
    "name": "Broken Shop",
    "from_contains": "@broken.example.com",
    "order_subject_regex": "(unclosed",
}


def test_defaults_need_no_configuration() -> None:
    settings, problems = parse_settings({})
    assert problems == []
    assert settings == Settings()
    assert settings.price_check_interval_seconds == DEFAULT_CHECK_INTERVAL == 21_600
    assert settings.min_fetch_interval_seconds == DEFAULT_MIN_FETCH_INTERVAL == 21_600
    assert settings.allows_host("anything.example.com")


def test_valid_rule_is_compiled_case_insensitively() -> None:
    settings, problems = parse_settings({"merchant_rules": [GOOD_RULE]})
    assert problems == []
    (rule,) = settings.merchant_rules
    assert rule.matches_sender("Example Outfitters <ship@orders.example.com>")
    assert rule.shipped_subject is not None
    assert rule.shipped_subject.search("Your order HAS SHIPPED")


def test_regex_that_does_not_compile_skips_the_rule_and_is_reported() -> None:
    settings, problems = parse_settings({"merchant_rules": [BROKEN_RULE, GOOD_RULE]})
    assert [rule.name for rule in settings.merchant_rules] == ["Example Outfitters"]
    assert len(problems) == 1
    assert "order_subject_regex does not compile" in problems[0]
    assert "Broken Shop" in problems[0]


def test_rule_without_subject_regex_is_skipped() -> None:
    rule = {"name": "Quiet Shop", "from_contains": "@quiet.example.com", "order_id_regex": "Q-[0-9]+"}
    settings, problems = parse_settings({"merchant_rules": [rule]})
    assert settings.merchant_rules == ()
    assert any("needs at least one" in problem for problem in problems)


def test_rule_typos_and_bad_currency_are_reported() -> None:
    rule = dict(GOOD_RULE, shiped_subject_regex="typo", currency="dollars")
    settings, problems = parse_settings({"merchant_rules": [rule]})
    assert settings.merchant_rules[0].currency is None
    assert any("unknown keys: shiped_subject_regex" in problem for problem in problems)
    assert any("currency must be a three-letter" in problem for problem in problems)


def test_allowed_domains_keep_sane_entries_and_report_the_rest() -> None:
    raw: dict[str, Any] = {
        "allowed_domains": [
            "Shop.Example.com",
            "https://bad.example.com/path",
            "*.example.org",
            "192.0.2.10",
            "localhost",
        ]
    }
    settings, problems = parse_settings(raw)
    assert settings.allowed_domains == ("shop.example.com",)
    assert len(problems) == 4
    assert settings.allows_host("shop.example.com")
    assert settings.allows_host("eu.shop.example.com")
    assert settings.allows_host("SHOP.EXAMPLE.COM.")
    assert not settings.allows_host("example.com")
    assert not settings.allows_host("badshop.example.com.evil.example.net")


def test_allowed_domains_of_the_wrong_type_block_everything() -> None:
    settings, problems = parse_settings({"allowed_domains": "shop.example.com"})
    assert settings.allowed_domains == ()
    assert not settings.allows_host("shop.example.com")
    assert len(problems) == 1
    assert problems[0].startswith("allowed_domains must be a list")


def test_intervals_fall_back_or_clamp_and_are_reported() -> None:
    settings, problems = parse_settings(
        {"price_check_interval_seconds": 10, "min_fetch_interval_seconds": "soon", "price_check_intervall": 5}
    )
    assert settings.price_check_interval_seconds == MIN_CHECK_INTERVAL
    assert settings.min_fetch_interval_seconds == DEFAULT_MIN_FETCH_INTERVAL
    assert len(problems) == 3
    assert problems[0] == "unknown setting 'price_check_intervall'"


def test_doctor_reports_problems_while_the_app_still_starts(make_app: AppFactory) -> None:
    app = make_app(
        {
            "switchboard": {"connectors": ["shopping"]},
            "connectors": {"shopping": {"merchant_rules": [BROKEN_RULE], "allowed_domains": ["localhost"]}},
        }
    )
    problems = app.connectors["shopping"].doctor(app.ctx)
    assert any("does not compile" in problem for problem in problems)
    assert any("allowed_domains entry 'localhost'" in problem for problem in problems)
    assert app.registry.get("shopping_watch_add") is not None


def test_doctor_is_quiet_for_a_good_configuration(make_app: AppFactory) -> None:
    app = make_app(
        {
            "switchboard": {"connectors": ["shopping"]},
            "connectors": {"shopping": {"merchant_rules": [GOOD_RULE], "allowed_domains": ["example.com"]}},
        }
    )
    assert app.connectors["shopping"].doctor(app.ctx) == []
