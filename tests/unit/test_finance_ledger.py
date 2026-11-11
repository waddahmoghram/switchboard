"""Ledger behavior through the runtime: idempotent imports, dedupe, pending matching, and masking."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from switchboard.app import App
from switchboard.connectors.finance.ledger import NewTransaction, TransactionQuery, row_keys
from switchboard.connectors.finance.privacy import last4_of, mask, scrub_numbers
from switchboard.connectors.finance.runtime import FinanceRuntime
from switchboard.policy import Origin

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
TODAY = date(2026, 11, 10)
ALERT_BODY = (
    r"A charge of \$(?P<amount>[\d,]+\.\d{2}) at (?P<merchant>.+?) on your card ending in (?P<last4>\d{4})"
)


def config(**finance: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        # A fixed offset keeps alert dates independent of the machine running the tests.
        "timezone": "-08:00",
        "accounts": {"everyday": {"last4": "6789", "kind": "checking"}},
        "csv_profiles": {
            "examplecu": {
                "account": "everyday",
                "files": "examplecu*.csv",
                "date_column": "Date",
                "description_column": "Description",
                "amount_column": "Amount",
                "date_format": "%m/%d/%Y",
            }
        },
        "alert_rules": [{"name": "card", "from": "alerts@examplecu.example.com", "body": ALERT_BODY}],
        "categories": {
            "income": ["payroll"],
            "groceries": ["example market"],
            "subscriptions": ["examplestream"],
            "dining": ["coffee", "bakery", "diner", "pizza"],
        },
    }
    settings.update(finance)
    return {"switchboard": {"connectors": ["finance"]}, "connectors": {"finance": settings}}


def alert(text: str, message_id: str, day: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "from": "Example Credit Union <alerts@examplecu.example.com>",
        "subject": "Card alert",
        "date": f"{day} 10:00:00 -0800",
        "text": text,
    }


def checking_ofx() -> bytes:
    return (FIXTURES / "example_cu_checking.ofx").read_bytes()


def all_rows(runtime: FinanceRuntime, **filters: Any) -> list[dict[str, Any]]:
    return runtime.transactions(TransactionQuery(limit=200, **filters))["transactions"]


def test_importing_the_same_file_twice_is_a_no_op(make_app: Callable[..., App]) -> None:
    runtime = FinanceRuntime(make_app(config()).ctx)

    first = runtime.import_bytes("checking.ofx", checking_ofx(), today=TODAY)
    second = runtime.import_bytes("checking-copy.ofx", checking_ofx(), today=TODAY)
    reissued = runtime.import_bytes(
        "reissued.ofx", checking_ofx().replace(b"<TRNUID>1", b"<TRNUID>2"), today=TODAY
    )

    assert (first.status, first.new, first.accounts) == ("imported", 4, ("everyday",))
    assert second.status == "duplicate"
    # A different file with the same FITIDs adds nothing.
    assert (reissued.status, reissued.new, reissued.duplicates) == ("imported", 0, 4)
    assert len(all_rows(runtime)) == 4


def test_identical_rows_on_the_same_day_are_kept_and_overlaps_deduplicated(
    make_app: Callable[..., App],
) -> None:
    runtime = FinanceRuntime(make_app(config()).ctx)
    two_coffees = (
        b"Date,Description,Amount\n11/03/2026,Example Coffee,-3.50\n11/03/2026,Example Coffee,-3.50\n"
    )
    overlap = two_coffees + b"11/04/2026,Example Bakery,-6.25\n"

    first = runtime.import_bytes("examplecu-1.csv", two_coffees, today=TODAY)
    second = runtime.import_bytes("examplecu-2.csv", overlap, today=TODAY)

    assert first.new == 2
    assert (second.new, second.duplicates) == (1, 2)
    assert [row["amount"] for row in all_rows(runtime)] == [-6.25, -3.5, -3.5]


def test_row_keys_count_occurrences_within_one_file() -> None:
    coffee = NewTransaction(
        date(2026, 11, 3), -350, "USD", "Example  Coffee", "EXAMPLE COFFEE", "dining", "csv"
    )
    same_but_spaced = NewTransaction(
        date(2026, 11, 3), -350, "USD", "example coffee", "EXAMPLE COFFEE", "dining", "csv"
    )

    first, second = row_keys(1, [coffee, same_but_spaced])

    assert first != second
    assert row_keys(1, [coffee]) == [first]
    assert row_keys(2, [coffee]) != [first]


def test_posted_transaction_confirms_a_pending_alert(make_app: Callable[..., App]) -> None:
    app = make_app(config())
    runtime = FinanceRuntime(app.ctx)
    text = "A charge of $15.49 at EXAMPLESTREAM.COM on your card ending in 6789."

    alert_id = runtime.handle_mail(alert(text, "<m1@example.com>", "Tue, 03 Nov 2026"), today=TODAY)
    assert alert_id is not None
    assert [row["source"] for row in all_rows(runtime, pending=True)] == ["alert"]

    result = runtime.import_bytes("checking.ofx", checking_ofx(), today=TODAY)

    assert result.matched_pending == 1
    assert all_rows(runtime, pending=True) == []
    [stream] = all_rows(runtime, merchant_contains="examplestream")
    assert (stream["source"], stream["pending"], stream["amount"]) == ("ofx", False, -15.49)
    assert runtime.summary("2026-11")["spending"] == 1215.49


def test_an_alert_after_the_statement_is_born_matched(make_app: Callable[..., App]) -> None:
    app = make_app(config())
    runtime = FinanceRuntime(app.ctx)
    runtime.import_bytes("checking.ofx", checking_ofx(), today=TODAY)
    text = "A charge of $15.49 at EXAMPLESTREAM.COM on your card ending in 6789."

    alert_id = runtime.handle_mail(alert(text, "<m2@example.com>", "Tue, 03 Nov 2026"), today=TODAY)

    assert alert_id is not None
    assert all_rows(runtime, pending=True) == []
    assert [event for event in app.ctx.events.recent(50) if event.type == "finance.transaction"] == []
    # The same email delivered again changes nothing.
    assert runtime.handle_mail(alert(text, "<m2@example.com>", "Tue, 03 Nov 2026"), today=TODAY) is None


def unnamed() -> dict[str, Any]:
    """The same setup before the user named the account ending in 6789."""
    return config(accounts={}, csv_profiles={})


def test_naming_an_account_after_a_statement_keeps_its_alerts_in_it(make_app: Callable[..., App]) -> None:
    FinanceRuntime(make_app(unnamed()).ctx).import_bytes("checking.ofx", checking_ofx(), today=TODAY)
    app = make_app(config())
    runtime = FinanceRuntime(app.ctx)
    text = "A charge of $15.49 at EXAMPLESTREAM.COM on your card ending in 6789."

    runtime.handle_mail(alert(text, "<m4@example.com>", "Tue, 03 Nov 2026"), today=TODAY)

    accounts = app.call("finance_accounts", {}, Origin.ASSISTANT).data["accounts"]
    assert [account["name"] for account in accounts] == ["everyday"]
    assert all_rows(runtime, pending=True) == []
    assert runtime.summary("2026-11")["spending"] == 1215.49


def test_naming_an_account_after_its_alerts_keeps_the_statement_with_them(
    make_app: Callable[..., App],
) -> None:
    text = "A charge of $15.49 at EXAMPLESTREAM.COM on your card ending in 6789."
    FinanceRuntime(make_app(unnamed()).ctx).handle_mail(
        alert(text, "<m5@example.com>", "Tue, 03 Nov 2026"), today=TODAY
    )
    app = make_app(config())
    runtime = FinanceRuntime(app.ctx)

    result = runtime.import_bytes("checking.ofx", checking_ofx(), today=TODAY)

    assert result.matched_pending == 1
    accounts = app.call("finance_accounts", {}, Origin.ASSISTANT).data["accounts"]
    assert [account["name"] for account in accounts] == ["everyday"]
    assert runtime.summary("2026-11")["spending"] == 1215.49


def test_matching_prefers_the_most_similar_merchant(make_app: Callable[..., App]) -> None:
    runtime = FinanceRuntime(make_app(config()).ctx)
    pizza = "A charge of $15.49 at EXAMPLE PIZZA on your card ending in 6789."
    stream = "A charge of $15.49 at EXAMPLESTREAM.COM on your card ending in 6789."
    runtime.handle_mail(alert(pizza, "<p@example.com>", "Tue, 03 Nov 2026"), today=TODAY)
    runtime.handle_mail(alert(stream, "<s@example.com>", "Mon, 02 Nov 2026"), today=TODAY)

    runtime.import_bytes("checking.ofx", checking_ofx(), today=TODAY)

    [still_pending] = all_rows(runtime, pending=True)
    assert still_pending["merchant"] == "EXAMPLE PIZZA"


def test_accounts_are_masked_and_numbers_never_stored(make_app: Callable[..., App]) -> None:
    app = make_app(config())
    FinanceRuntime(app.ctx).import_bytes("checking.ofx", checking_ofx(), today=TODAY)

    outcome = app.call("finance_accounts", {}, Origin.ASSISTANT)

    assert outcome.status == "ok"
    assert outcome.data["accounts"] == [
        {
            "name": "everyday",
            "number": "****6789",
            "kind": "checking",
            "currency": "USD",
            "balance": 3200.39,
            "available_balance": 3100.39,
            "balance_as_of": "2026-11-05",
            "transactions": 4,
            "last_activity": "2026-11-04",
        }
    ]
    with app.ctx.db.session() as conn:
        stored = [
            dict(row)
            for query in (
                "SELECT * FROM finance_accounts",
                "SELECT * FROM finance_transactions",
                "SELECT * FROM finance_imports",
                "SELECT * FROM events",
                "SELECT * FROM kv",
            )
            for row in conn.execute(query)
        ]
    dump = json.dumps(stored) + app.ctx.audit.path.read_text(encoding="utf-8")
    assert "123456789" not in dump
    assert "ECU-20261015-001" not in dump
    salt = app.ctx.db.kv_get("finance", "identity_salt")
    assert salt is not None and len(salt) == 32


def test_masking_helpers() -> None:
    assert last4_of("000123456789") == "6789"
    assert last4_of("1234") == ""
    assert mask("6789") == "****6789"
    assert mask("") == "****"
    assert scrub_numbers("ONLINE TRANSFER TO 000123456789") == "ONLINE TRANSFER TO ****6789"
    assert scrub_numbers("CARD 4000 0000 0000 4321 PURCHASE") == "CARD ****4321 PURCHASE"
    assert scrub_numbers("PAID 2026-11-05 REF 12345678") == "PAID 2026-11-05 REF 12345678"


def test_budget_event_fires_once_per_category_per_month(make_app: Callable[..., App]) -> None:
    app = make_app(config(budgets={"dining": 10}))
    runtime = FinanceRuntime(app.ctx)
    november = b"Date,Description,Amount\n11/03/2026,Example Coffee,-6.00\n11/04/2026,Example Bakery,-5.00\n"
    runtime.import_bytes("examplecu-nov.csv", november, today=date(2026, 11, 15))

    first = runtime.check_budgets(date(2026, 11, 15))
    again = runtime.check_budgets(date(2026, 11, 20))
    december_before_spending = runtime.check_budgets(date(2026, 12, 1))
    runtime.import_bytes(
        "examplecu-dec.csv", b"Date,Description,Amount\n12/02/2026,Example Diner,-12.00\n", today=TODAY
    )
    december = runtime.check_budgets(date(2026, 12, 15))

    assert first == [
        {"category": "dining", "month": "2026-11", "spent": 11.0, "limit": 10.0, "currency": "USD"}
    ]
    assert again == []
    assert december_before_spending == []
    assert [item["month"] for item in december] == ["2026-12"]
    events = [event for event in app.ctx.events.recent(50) if event.type == "finance.budget_exceeded"]
    assert [event.payload["month"] for event in events] == ["2026-12", "2026-11"]
