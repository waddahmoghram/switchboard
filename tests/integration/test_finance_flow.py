"""End to end: inbox import, ledger tools, events, a bank alert email, and a rule reacting to it."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.finance.runtime import FinanceRuntime
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
FULL_NUMBERS = ("000123456789", "4000000000004321")


def finance_config(inbox: Path) -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["finance"]},
        "connectors": {
            "finance": {
                "import_dir": str(inbox),
                "accounts": {
                    "everyday": {"last4": "6789", "kind": "checking"},
                    "card": {"last4": "4321", "kind": "credit_card"},
                    "savings": {"kind": "savings", "csv_profile": "examplecu"},
                },
                "csv_profiles": {
                    "examplecu": {
                        "files": "examplecu-*.csv",
                        "date_column": "Date",
                        "description_column": "Description",
                        "amount_column": "Amount",
                        "date_format": "%m/%d/%Y",
                    }
                },
                "alert_rules": [
                    {
                        "name": "example card charge",
                        "from": "alerts@examplecu.example.com",
                        "subject": "(?i)transaction alert",
                        "body": (
                            r"A charge of \$(?P<amount>[\d,]+\.\d{2}) at (?P<merchant>.+?) "
                            r"on your card ending in (?P<last4>\d{4})"
                        ),
                    }
                ],
                "categories": {
                    "income": ["payroll"],
                    "groceries": ["example market"],
                    "subscriptions": ["examplestream"],
                    "home": ["hardware"],
                    "dining": ["diner"],
                    "transfer": ["payment thank you", "transfer from"],
                },
                "budgets": {"dining": 5},
            }
        },
        "rules": [
            {
                "name": "record card alerts",
                "on": "finance.transaction",
                "match": {"payload.source": "alert"},
                "do": [
                    {
                        "tool": "test_record_transaction",
                        "args": {
                            "merchant": "{payload.merchant}",
                            "amount": "{payload.amount}",
                            "account": "{payload.account}",
                        },
                    }
                ],
            }
        ],
    }


def add_recorder(app: App) -> list[dict[str, Any]]:
    """Register a test-only tool that the rule calls, and return what it receives."""
    received: list[dict[str, Any]] = []

    def record(args: dict[str, Any]) -> dict[str, Any]:
        received.append(args)
        return {"recorded": len(received)}

    app.registry.add(
        Tool(
            "test_record_transaction",
            "Test-only: remember a transaction.",
            Tier.READ,
            {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string", "maxLength": 64},
                    "amount": {"type": "number"},
                    "account": {"type": "string", "maxLength": 64},
                },
                "required": ["merchant", "amount", "account"],
                "additionalProperties": False,
            },
            record,
        )
    )
    return received


def call(app: App, tool: str, args: dict[str, Any] | None = None) -> Any:
    outcome = app.call(tool, args or {}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    return outcome.data


def events_of(app: App, event_type: str) -> list[dict[str, Any]]:
    return [event.payload for event in app.ctx.events.recent(200) if event.type == event_type]


def test_inbox_import_tools_events_alert_and_rule(make_app: Callable[..., App], tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    shutil.copy(FIXTURES / "example_cu_checking.ofx", inbox / "example_cu_checking.ofx")
    shutil.copy(FIXTURES / "example_card.qfx", inbox / "example_card.qfx")
    (inbox / "examplecu-savings.csv").write_bytes(
        b"\xef\xbb\xbf" + (FIXTURES / "example_cu_savings.csv").read_bytes()
    )
    (inbox / "broken.ofx").write_bytes(b"this is not a statement")
    (inbox / "notes.txt").write_text("not a statement", encoding="utf-8")
    app = make_app(finance_config(inbox))
    received = add_recorder(app)

    # 1. Import the inbox through the policy gate, as an assistant would.
    imported = call(app, "finance_import_inbox")

    statuses = {item["file"]: item["status"] for item in imported["files"]}
    assert statuses == {
        "notes.txt": "skipped",
        "broken.ofx": "failed",
        "example_card.qfx": "imported",
        "example_cu_checking.ofx": "imported",
        "examplecu-savings.csv": "imported",
    }
    assert imported["new_transactions"] == 9
    assert (imported["events_emitted"], imported["events_not_emitted"]) == (9, 0)
    assert sorted(path.name for path in (inbox / "processed").iterdir()) == [
        "example_card.qfx",
        "example_cu_checking.ofx",
        "examplecu-savings.csv",
    ]
    assert [path.name for path in (inbox / "failed").iterdir()] == ["broken.ofx"]
    assert (inbox / "notes.txt").exists()

    # 2. Ledger queries through the tools.
    accounts = {account["name"]: account for account in call(app, "finance_accounts")["accounts"]}
    assert (accounts["everyday"]["number"], accounts["everyday"]["balance"]) == ("****6789", 3200.39)
    assert (accounts["card"]["number"], accounts["card"]["balance"]) == ("****4321", -512.34)
    assert (accounts["savings"]["number"], accounts["savings"]["transactions"]) == ("****", 3)

    [hardware] = call(app, "finance_transactions", {"merchant_contains": "smith"})["transactions"]
    assert (hardware["account"], hardware["amount"], hardware["category"]) == ("card", -42.5, "home")
    big = call(app, "finance_transactions", {"max_amount": -100})["transactions"]
    assert [row["merchant"] for row in big] == ["CHECK"]
    assert call(app, "finance_transactions", {"account": "savings"})["count"] == 3

    november = call(app, "finance_summary", {"month": "2026-11"})
    assert (november["income"], november["spending"]) == (0.0, 1257.99)
    assert november["transfers"] == {"in": 1300.0, "out": 0.0}
    assert call(app, "finance_recurring")["recurring"] == []

    # 3. Events for the new transactions.
    transactions = events_of(app, "finance.transaction")
    assert len(transactions) == 9
    payroll = next(payload for payload in transactions if payload["category"] == "income")
    assert {key: payroll[key] for key in ("account", "date", "amount", "currency", "pending", "source")} == {
        "account": "everyday",
        "date": "2026-10-15",
        "amount": 2500.0,
        "currency": "USD",
        "pending": False,
        "source": "ofx",
    }

    # 4. Importing the same statement again is a no-op.
    shutil.copy(FIXTURES / "example_cu_checking.ofx", inbox / "again.ofx")
    again = call(app, "finance_import_inbox")
    assert [item["status"] for item in again["files"]] == ["skipped", "duplicate"]
    assert len(events_of(app, "finance.transaction")) == 9

    # 5. A bank alert email arrives through the mail connector's event.
    mail = {
        "account": "personal",
        "mailbox": "INBOX",
        "uid": 42,
        "message_id": "<alert-42@examplecu.example.com>",
        "from": "Example Credit Union <alerts@examplecu.example.com>",
        "to": ["you@example.com"],
        "cc": [],
        "subject": "Transaction alert",
        "date": format_datetime(datetime.now().astimezone()),
        "text": "A charge of $12.50 at EXAMPLE DINER on your card ending in 4321.",
        "has_attachments": False,
        "attachments": [],
    }
    app.ctx.events.emit("mail", "mail.received", mail, dedupe_key="mail:personal:1:42")
    app.process_events()

    [pending] = call(app, "finance_transactions", {"pending": True})["transactions"]
    assert {key: pending[key] for key in ("account", "amount", "merchant", "source", "category")} == {
        "account": "card",
        "amount": -12.5,
        "merchant": "EXAMPLE DINER",
        "source": "alert",
        "category": "dining",
    }
    # 6. The rule engine saw the derived event and called the test tool.
    assert received == [{"merchant": "EXAMPLE DINER", "amount": -12.5, "account": "card"}]
    budget = events_of(app, "finance.budget_exceeded")
    assert budget == [
        {
            "category": "dining",
            "month": f"{date.today():%Y-%m}",
            "spent": 12.5,
            "limit": 5.0,
            "currency": "USD",
        }
    ]
    with app.ctx.db.session() as conn:
        failures = conn.execute("SELECT COUNT(*) AS n FROM events WHERE error IS NOT NULL").fetchone()["n"]
    assert failures == 0

    # 7. The same email delivered twice adds nothing.
    app.ctx.events.emit("mail", "mail.received", mail, dedupe_key="mail:personal:1:42:redelivered")
    app.process_events()
    assert call(app, "finance_transactions", {"pending": True})["count"] == 1
    assert len(received) == 1

    # 8. No full account number appears in anything an assistant or a rule can see.
    visible = json.dumps(
        [
            imported,
            call(app, "finance_accounts"),
            call(app, "finance_transactions", {"limit": 200}),
            [event.payload for event in app.ctx.events.recent(200) if event.source == "finance"],
        ]
    )
    for number in FULL_NUMBERS:
        assert number not in visible


def test_an_unexpected_failure_leaves_the_file_and_imports_the_rest(
    make_app: Callable[..., App], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    shutil.copy(FIXTURES / "example_card.qfx", inbox / "a_locked.qfx")
    shutil.copy(FIXTURES / "example_cu_checking.ofx", inbox / "b_checking.ofx")
    app = make_app(finance_config(inbox))
    original = FinanceRuntime.import_bytes

    def locked_once(self: FinanceRuntime, name: str, raw: bytes, **kwargs: Any) -> Any:
        if name == "a_locked.qfx":
            raise sqlite3.OperationalError("database is locked")
        return original(self, name, raw, **kwargs)

    monkeypatch.setattr(FinanceRuntime, "import_bytes", locked_once)

    imported = call(app, "finance_import_inbox")

    results = {item["file"]: item for item in imported["files"]}
    assert (results["a_locked.qfx"]["status"], results["b_checking.ofx"]["status"]) == ("error", "imported")
    assert "OperationalError" in results["a_locked.qfx"]["error"]
    assert imported["events_emitted"] == imported["new_transactions"] == 4
    assert (inbox / "a_locked.qfx").exists()
    assert [path.name for path in (inbox / "processed").iterdir()] == ["b_checking.ofx"]
