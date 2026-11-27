"""Configuration checks and ``switchboard doctor`` for the finance connector."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from datetime import UTC, timedelta, timezone
from pathlib import Path
from typing import Any

from switchboard.app import App
from switchboard.connectors import finance
from switchboard.connectors.finance.merchants import categorize, normalize_merchant, similarity
from switchboard.connectors.finance.settings import date_format_problem, load_settings
from switchboard.policy import Origin


def app_with(make_app: Callable[..., App], **settings: Any) -> App:
    return make_app({"switchboard": {"connectors": ["finance"]}, "connectors": {"finance": settings}})


def test_valid_configuration_has_no_problems() -> None:
    settings = load_settings(
        {
            "currency": "EUR",
            "accounts": {"everyday": {"last4": "6789", "csv_profile": "examplecu"}},
            "csv_profiles": {
                "examplecu": {
                    "date_column": "Date",
                    "description_column": "Description",
                    "debit_column": "Debit",
                    "credit_column": "Credit",
                    "date_format": ["%d.%m.%Y", "%Y-%m-%d"],
                    "delimiter": ";",
                    "decimal_mark": ",",
                    "skip_rows": 3,
                }
            },
            "categories": {"groceries": ["example market"]},
            "budgets": {"groceries": 400.5},
            "transfer_categories": ["transfer", "savings"],
        }
    )

    assert settings.problems == ()
    assert settings.currency == "EUR"
    assert settings.budgets == {"groceries": 40050}
    assert settings.account_for_profile(settings.csv_profiles["examplecu"]) == "everyday"
    assert settings.csv_profiles["examplecu"].date_formats == ("%d.%m.%Y", "%Y-%m-%d")
    assert settings.transfer_categories == frozenset({"transfer", "savings"})


def test_configuration_problems_are_collected() -> None:
    settings = load_settings(
        {
            "currency": "dollars",
            "accounts": {
                "one": {"last4": "1234"},
                "two": {"last4": "1234"},
                "three": {"last4": 1234},
                "four": {"csv_profile": "missing"},
            },
            "csv_profiles": {
                "both": {
                    "date_column": "Date",
                    "description_column": "Description",
                    "amount_column": "Amount",
                    "debit_column": "Debit",
                    "credit_column": "Credit",
                },
                "no_year": {
                    "date_column": "Date",
                    "description_column": "Description",
                    "amount_column": "Amount",
                    "date_format": "%m/%d",
                },
                "typo": {
                    "date_column": "Date",
                    "description_column": "Description",
                    "amount_column": "Amount",
                    "colour": "blue",
                },
            },
            "categories": {"bad": ["(unclosed"]},
            "budgets": {"groceries": -5, "travel": 100},
        }
    )
    problems = "\n".join(settings.problems)

    assert "currency must be a three-letter currency code" in problems
    assert "accounts.two and accounts.one both end in 1234" in problems
    assert "accounts.three: last4 must be four digits in quotes" in problems
    assert "accounts.four: csv_profile 'missing' is not defined" in problems
    assert "csv_profiles.both: set amount_column, or debit_column and credit_column, but not both" in problems
    assert "csv_profiles.no_year: date_format '%m/%d' must include a year, a month, and a day" in problems
    assert "csv_profiles.typo: unknown key(s) colour" in problems
    assert "categories.bad[0] does not compile" in problems
    assert "budgets.groceries must be a positive monthly amount" in problems
    assert "budgets.travel has no [connectors.finance.categories] entry" in problems
    assert settings.currency == "USD"
    # Broken profiles are dropped; an unknown key alone is only reported.
    assert sorted(settings.csv_profiles) == ["typo"]


def test_date_format_problem() -> None:
    assert date_format_problem("%Y-%m-%d") is None
    assert date_format_problem("%d %b %Y") is None
    assert date_format_problem("%m/%d") is not None


def test_date_format_problem_does_not_parse_a_day_without_a_year() -> None:
    # Python 3.13 deprecates that parse. strptime warns only when it first compiles a format, so these
    # formats appear in no other test.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert (
            date_format_problem("%d of %B")
            == "date_format '%d of %B' must include a year, a month, and a day"
        )
        assert date_format_problem("%d of %Q") == "date_format '%d of %Q' is not a valid strptime format"


def test_date_format_problem_reports_a_repeated_directive() -> None:
    assert date_format_problem("%d/%m/%Y %d") == "date_format '%d/%m/%Y %d' is not a valid strptime format"


def test_timezone_setting() -> None:
    assert load_settings({}).zone is None
    assert load_settings({"timezone": "UTC"}).zone == UTC
    assert load_settings({"timezone": "-08:00"}).zone == timezone(-timedelta(hours=8))
    assert load_settings({"timezone": "+05:30"}).zone == timezone(timedelta(hours=5, minutes=30))
    for bad in ("Mars/Olympus_Mons", "", "-25:00", 8):
        settings = load_settings({"timezone": bad})
        assert settings.zone is None
        assert any(problem.startswith("timezone must be") for problem in settings.problems)


def test_doctor_accepts_a_private_inbox(make_app: Callable[..., App], tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)

    assert finance.doctor(app_with(make_app, import_dir=str(inbox)).ctx) == []


def test_doctor_and_tool_refuse_a_symlinked_inbox(make_app: Callable[..., App], tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    app = app_with(make_app, import_dir=str(link))

    problems = finance.doctor(app.ctx)
    outcome = app.call("finance_import_inbox", {}, Origin.ASSISTANT)

    assert any("is a symbolic link" in problem for problem in problems)
    assert outcome.status == "error"
    assert "symbolic link" in outcome.message


def test_doctor_and_tool_refuse_an_inbox_others_can_write(
    make_app: Callable[..., App], tmp_path: Path
) -> None:
    inbox = tmp_path / "shared"
    inbox.mkdir()
    inbox.chmod(0o777)  # The unsafe mode is what this test checks for.
    app = app_with(make_app, import_dir=str(inbox))

    problems = finance.doctor(app.ctx)
    outcome = app.call("finance_import_inbox", {}, Origin.ASSISTANT)

    assert any("writable by other users" in problem for problem in problems)
    assert outcome.status == "error"


def test_doctor_warns_when_others_can_read_the_inbox(make_app: Callable[..., App], tmp_path: Path) -> None:
    inbox = tmp_path / "readable"
    inbox.mkdir()
    inbox.chmod(0o755)  # The readable mode is what this test checks for.
    app = app_with(make_app, import_dir=str(inbox))

    assert any("readable by other users" in problem for problem in finance.doctor(app.ctx))
    assert app.call("finance_import_inbox", {}, Origin.ASSISTANT).status == "ok"


def test_doctor_reports_missing_inbox_and_missing_mail_connector(
    make_app: Callable[..., App], tmp_path: Path
) -> None:
    app = app_with(
        make_app,
        import_dir=str(tmp_path / "nowhere"),
        alert_rules=[{"from": "alerts@example.com", "body": r"(?P<amount>\d+\.\d{2})"}],
    )

    problems = finance.doctor(app.ctx)

    assert any("does not exist" in problem for problem in problems)
    assert "alert_rules need the mail connector, which is not enabled" in problems


def test_doctor_reports_an_idle_connector(make_app: Callable[..., App]) -> None:
    assert finance.doctor(app_with(make_app).ctx) == [
        "set import_dir or alert_rules; otherwise nothing reaches the ledger"
    ]


def test_merchant_normalization() -> None:
    assert normalize_merchant("SQ *BLUE BOTTLE #0123") == "BLUE BOTTLE"
    assert normalize_merchant("POS DEBIT 1105 EXAMPLE MARKET") == "EXAMPLE MARKET"
    assert normalize_merchant("TST* Example Cafe") == "EXAMPLE CAFE"
    assert normalize_merchant("AMAZON.COM*AB12CD3") == "AMAZON.COM"
    assert normalize_merchant("Example Streaming 866-555-0100 CA") == "EXAMPLE STREAMING CA"
    assert normalize_merchant("SPOTLIGHT THEATER") == "SPOTLIGHT THEATER"
    assert normalize_merchant("12345") == "12345"
    assert normalize_merchant("") == ""


def test_categories_follow_configuration_order_and_ignore_case() -> None:
    rules = load_settings(
        {"categories": {"subscriptions": ["examplestream"], "entertainment": ["stream", "theater"]}}
    ).categories

    assert categorize(rules, "EXAMPLESTREAM.COM") == "subscriptions"
    assert categorize(rules, "OTHER STREAM") == "entertainment"
    assert categorize(rules, "", "Spotlight Theater tickets") == "entertainment"
    assert categorize(rules, "EXAMPLE MARKET") == "uncategorized"


def test_similarity() -> None:
    assert similarity("EXAMPLESTREAM.COM", "EXAMPLESTREAM.COM") == 1.0
    assert similarity("EXAMPLESTREAM.COM", "") == 0.0
    assert similarity("EXAMPLESTREAM.COM", "EXAMPLESTREAM") > similarity("EXAMPLESTREAM.COM", "EXAMPLE PIZZA")
