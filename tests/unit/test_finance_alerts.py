"""Bank alert email rules."""

from __future__ import annotations

from datetime import UTC, date, timedelta, timezone
from typing import Any

import pytest

from switchboard.connectors.finance.alerts import header_date, match_alert, message_key, sender_address
from switchboard.connectors.finance.models import FormatError
from switchboard.connectors.finance.settings import load_settings

TODAY = date(2026, 11, 10)
PACIFIC = timezone(-timedelta(hours=8))

SETTINGS = load_settings(
    {
        "alert_rules": [
            {
                "name": "example card charge",
                "from": "alerts@examplecu.example.com",
                "subject": "(?i)transaction alert",
                "body": (
                    r"A charge of \$(?P<amount>[\d,]+\.\d{2}) at (?P<merchant>[^.]+?) "
                    r"on (?P<date>\d{2}/\d{2}/\d{4}) on your card ending in (?P<last4>\d{4})"
                ),
            },
            {
                "name": "example deposit",
                "from": ["*@examplecu.example.com"],
                "body": r"Deposit of \$(?P<amount>[\d,]+\.\d{2})",
                "direction": "in",
                "account": "everyday",
            },
        ]
    }
)


def mail(text: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account": "personal",
        "mailbox": "INBOX",
        "uid": 7,
        "message_id": "<a1@examplecu.example.com>",
        "from": "Example Credit Union <alerts@examplecu.example.com>",
        "to": ["you@example.com"],
        "cc": [],
        "subject": "Transaction alert",
        "date": "Thu, 05 Nov 2026 18:30:00 -0800",
        "text": text,
        "has_attachments": False,
        "attachments": [],
    }
    payload.update(overrides)
    return payload


def test_rules_load_without_problems() -> None:
    assert SETTINGS.problems == ()
    assert [rule.name for rule in SETTINGS.alert_rules] == ["example card charge", "example deposit"]


def test_card_charge_alert() -> None:
    text = "A charge of $1,042.50 at EXAMPLE OUTFITTERS on 11/04/2026 on your card ending in 4321."

    found = match_alert(SETTINGS.alert_rules, mail(text), today=TODAY)

    assert found is not None
    assert found.rule == "example card charge"
    assert (found.amount_cents, found.merchant, found.last4) == (-104250, "EXAMPLE OUTFITTERS", "4321")
    assert found.posted == date(2026, 11, 4)
    assert found.account == ""
    assert found.message_key == message_key(mail(text))


def test_deposit_alert_is_money_in_and_dated_by_the_header() -> None:
    payload = mail("Deposit of $250.00 was posted.")

    found = match_alert(SETTINGS.alert_rules, payload, today=TODAY, zone=PACIFIC)

    assert found is not None
    assert (found.rule, found.amount_cents, found.account) == ("example deposit", 25000, "everyday")
    assert found.posted == date(2026, 11, 5)


def test_header_dates_use_the_calendar_date_in_the_configured_zone() -> None:
    # The mail connector sends UTC moments; 02:30 UTC on November 6 is still November 5 in California.
    assert header_date("2026-11-06T02:30:00+00:00", PACIFIC) == date(2026, 11, 5)
    assert header_date("2026-11-06T02:30:00+00:00", UTC) == date(2026, 11, 6)
    assert header_date("Thu, 05 Nov 2026 18:30:00 -0800", UTC) == date(2026, 11, 6)
    # "-0000" means the zone is unknown, so the date is kept as written.
    assert header_date("Thu, 05 Nov 2026 18:30:00 -0000", UTC) == date(2026, 11, 5)
    assert header_date("2026-11-05", PACIFIC) == date(2026, 11, 5)
    assert header_date("not a date") is None
    assert header_date(None) is None


def test_missing_date_falls_back_to_today() -> None:
    found = match_alert(SETTINGS.alert_rules, mail("Deposit of $1.00", date=""), today=TODAY)

    assert found is not None
    assert found.posted == TODAY


def test_other_senders_and_subjects_do_not_match() -> None:
    charge = "A charge of $5.00 at EXAMPLE CAFE on 11/04/2026 on your card ending in 4321."

    assert (
        match_alert(SETTINGS.alert_rules, mail(charge, **{"from": "x@lookalike.example.net"}), today=TODAY)
        is None
    )
    assert match_alert(SETTINGS.alert_rules, mail(charge, subject="Your statement"), today=TODAY) is None
    assert match_alert(SETTINGS.alert_rules, mail("Hello"), today=TODAY) is None


def test_sender_formats() -> None:
    assert sender_address("Example CU <Alerts@ExampleCU.example.com>") == "alerts@examplecu.example.com"
    assert sender_address(["alerts@examplecu.example.com"]) == "alerts@examplecu.example.com"
    assert sender_address({"name": "Example CU", "address": "alerts@examplecu.example.com"}) == (
        "alerts@examplecu.example.com"
    )
    assert sender_address(None) == ""


def test_message_key_without_message_id_is_stable() -> None:
    first = mail("Deposit of $1.00", message_id=None)

    assert message_key(first) == message_key(dict(first))
    assert message_key(first) != message_key(mail("Deposit of $2.00", message_id=None))


def test_unreadable_amount_group_is_a_format_error() -> None:
    rules = load_settings(
        {"alert_rules": [{"from": "*@example.com", "body": r"(?P<amount>\S+) charged"}]}
    ).alert_rules

    with pytest.raises(FormatError, match="amount group is not a number"):
        match_alert(rules, mail("lots charged", **{"from": "bank@example.com"}), today=TODAY)


def test_broken_rules_are_reported_and_dropped() -> None:
    settings = load_settings(
        {
            "alert_rules": [
                {"name": "broken", "from": "a@example.com", "body": "(unclosed"},
                {"name": "no amount", "from": "a@example.com", "body": r"charged (?P<value>\d+)"},
                {"name": "no sender", "body": r"(?P<amount>\d+)"},
                {
                    "name": "bad direction",
                    "from": "a@example.com",
                    "body": r"(?P<amount>\d+)",
                    "direction": "up",
                },
            ]
        }
    )

    assert settings.alert_rules == ()
    problems = "\n".join(settings.problems)
    assert "alert rule 'broken': body does not compile" in problems
    assert "alert rule 'no amount': body needs a named group (?P<amount>...)" in problems
    assert "alert rule 'no sender': 'from' must name" in problems
    assert "alert rule 'bad direction': direction must be" in problems
