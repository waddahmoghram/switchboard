"""Tool schemas, argument handling, and the ``switchboard finance`` command line."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.cli import build_parser
from switchboard.connectors import finance
from switchboard.connectors.finance.cli import render_summary
from switchboard.connectors.finance.inbox import read_file
from switchboard.connectors.finance.models import FormatError
from switchboard.connectors.finance.tools import transaction_query
from switchboard.errors import ToolError
from switchboard.policy import Origin, Tier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
CONFIG: dict[str, Any] = {
    "switchboard": {"connectors": ["finance"]},
    "connectors": {
        "finance": {
            "accounts": {"everyday": {"last4": "6789"}},
            "categories": {"income": ["payroll"], "groceries": ["example market"]},
        }
    },
}


def test_tools_are_read_or_draft_and_never_spend(make_app: Callable[..., App]) -> None:
    tools = {tool.name: tool for tool in make_app(CONFIG).registry.all() if tool.connector == "finance"}

    assert {name: tool.tier for name, tool in tools.items()} == {
        "finance_accounts": Tier.READ,
        "finance_transactions": Tier.READ,
        "finance_summary": Tier.READ,
        "finance_recurring": Tier.READ,
        "finance_import_inbox": Tier.DRAFT,
    }
    for tool in tools.values():
        assert tool.input_schema["additionalProperties"] is False
    assert "cannot move money" in tools["finance_import_inbox"].description


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 201},
        {"limit": 0},
        {"start": "11/05/2026"},
        {"merchant_contains": "x" * 65},
        {"min_amount": 2_000_000_000},
        {"unexpected": True},
    ],
)
def test_out_of_bounds_arguments_are_rejected(make_app: Callable[..., App], args: dict[str, Any]) -> None:
    assert make_app(CONFIG).call("finance_transactions", args, Origin.ASSISTANT).status == "invalid"


def test_transaction_query_converts_amounts_and_dates() -> None:
    query = transaction_query(
        {"start": "2026-11-01", "max_amount": -50, "min_amount": -100.25, "pending": False}
    )

    assert (query.start.isoformat() if query.start else None, query.min_cents, query.max_cents) == (
        "2026-11-01",
        -10025,
        -5000,
    )
    assert (query.pending, query.limit) == (False, 50)
    with pytest.raises(ToolError, match="start must not be after end"):
        transaction_query({"start": "2026-11-02", "end": "2026-11-01"})
    with pytest.raises(ToolError, match="real date"):
        transaction_query({"start": "2026-02-30"})


def test_summary_rejects_impossible_months(make_app: Callable[..., App]) -> None:
    app = make_app(CONFIG)

    assert app.call("finance_summary", {"month": "2026-13"}, Origin.ASSISTANT).status == "invalid"
    assert app.call("finance_summary", {"month": "2026-11"}, Origin.ASSISTANT).status == "ok"


def test_inbox_tool_needs_an_import_dir(make_app: Callable[..., App]) -> None:
    outcome = make_app(CONFIG).call("finance_import_inbox", {}, Origin.ASSISTANT)

    assert (outcome.status, outcome.message) == (
        "error",
        "set import_dir in [connectors.finance] to use the import inbox",
    )


def test_cli_import_and_summary(
    make_app: Callable[..., App], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(CONFIG)
    statement = tmp_path / "checking.ofx"
    statement.write_bytes((FIXTURES / "example_cu_checking.ofx").read_bytes())
    parser = build_parser([finance])

    imported = parser.parse_args(["finance", "import", str(statement)])
    assert imported.app_handler(imported, app) == 0
    printed = capsys.readouterr().out
    summary = parser.parse_args(["finance", "summary", "--month", "2026-10"])
    assert summary.app_handler(summary, app) == 0
    table = capsys.readouterr().out

    assert "checking.ofx: imported into everyday" in printed
    assert '"new": 4' in printed
    assert statement.exists()
    assert "income 2,500.00   spending 84.12   net 2,415.88" in table
    assert any(line.startswith("groceries") for line in table.splitlines())


def test_cli_import_reports_unreadable_files(make_app: Callable[..., App], tmp_path: Path) -> None:
    broken = tmp_path / "broken.ofx"
    broken.write_bytes(b"not a statement")
    parser = build_parser([finance])
    args = parser.parse_args(["finance", "import", str(broken)])

    with pytest.raises(ToolError, match=re.escape("broken.ofx: not an OFX file")):
        args.app_handler(args, make_app(CONFIG))


def test_reading_a_directory_is_a_format_error_and_closes_it(
    tmp_path: Path, open_descriptors: Callable[[], int]
) -> None:
    folder = tmp_path / "statement.ofx"
    folder.mkdir()
    baseline = open_descriptors()
    with pytest.raises(FormatError, match="not a regular file"):
        read_file(folder, follow_symlinks=True)
    assert open_descriptors() == baseline


def test_render_summary_marks_transfers_and_budgets() -> None:
    text = render_summary(
        {
            "month": "2026-11",
            "currency": "USD",
            "income": 0.0,
            "spending": 12.5,
            "net": -12.5,
            "categories": [
                {"category": "dining", "spending": 12.5, "income": 0.0, "budget": 10.0, "remaining": -2.5},
                {"category": "transfer", "spending": 0.0, "income": 300.0, "transfer": True},
            ],
        }
    )

    assert "dining" in text and "-2.50" in text
    assert "transfer (transfer)" in text
