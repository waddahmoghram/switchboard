"""Finance tools. There are deliberately no tools that move money: no SPEND tier, no payments."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from switchboard.connectors.finance.ledger import TransactionQuery
from switchboard.connectors.finance.models import NAME
from switchboard.connectors.finance.money import cents_from_number
from switchboard.connectors.finance.runtime import MAX_EVENTS_PER_IMPORT, FinanceRuntime
from switchboard.connectors.finance.settings import NAME_PATTERN
from switchboard.errors import ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_MAX_ABS_AMOUNT = 1_000_000_000
_NO_MONEY_MOVEMENT = "It cannot move money: this connector has no payment or transfer tools."

_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "maxLength": 10}
_AMOUNT = {"type": "number", "minimum": -_MAX_ABS_AMOUNT, "maximum": _MAX_ABS_AMOUNT}
_LABEL = {"type": "string", "minLength": 1, "maxLength": 64}

TRANSACTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start": {**_DATE, "description": "First date to include, YYYY-MM-DD."},
        "end": {**_DATE, "description": "Last date to include, YYYY-MM-DD."},
        "account": {**_LABEL, "pattern": NAME_PATTERN, "description": "Account name from finance_accounts."},
        "category": {**_LABEL, "description": "Exact category name, for example groceries."},
        "merchant_contains": {
            **_LABEL,
            "description": "Case-insensitive text in the merchant or description.",
        },
        "min_amount": {**_AMOUNT, "description": "Signed lower bound; negative is money out."},
        "max_amount": {**_AMOUNT, "description": "Signed upper bound; purchases of 50 or more: -50."},
        "pending": {"type": "boolean", "description": "true: only unconfirmed alerts; false: only posted."},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description": "Default 50."},
    },
    "additionalProperties": False,
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "month": {
            "type": "string",
            "pattern": r"^\d{4}-(0[1-9]|1[0-2])$",
            "maxLength": 7,
            "description": "YYYY-MM; default: the current month.",
        }
    },
    "additionalProperties": False,
}

RECURRING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_lapsed": {"type": "boolean", "description": "Also list charges that stopped recurring."}
    },
    "additionalProperties": False,
}


def register_tools(
    runtime: FinanceRuntime, registry: ToolRegistry, *, today: Callable[[], date] | None = None
) -> None:
    """Add the five finance tools; ``today`` defaults to the runtime's clock and can be fixed in tests."""
    clock = today or runtime.today

    def accounts(_args: dict[str, Any]) -> dict[str, Any]:
        return runtime.accounts()

    def transactions(args: dict[str, Any]) -> dict[str, Any]:
        return runtime.transactions(transaction_query(args))

    def summary(args: dict[str, Any]) -> dict[str, Any]:
        return runtime.summary(str(args.get("month") or f"{clock():%Y-%m}"))

    def recurring(args: dict[str, Any]) -> dict[str, Any]:
        return runtime.recurring(today=clock(), include_lapsed=bool(args.get("include_lapsed", False)))

    def import_inbox(_args: dict[str, Any]) -> dict[str, Any]:
        return runtime.import_inbox(today=clock())

    registry.add(
        Tool(
            "finance_accounts",
            "List the user's bank and card accounts in the local ledger, with masked numbers (last four "
            "digits only) and the balance from the newest imported statement. Balances are as of the "
            f"statement date, not live. Read-only. {_NO_MONEY_MOVEMENT}",
            Tier.READ,
            _EMPTY,
            accounts,
            title="Finance accounts",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "finance_transactions",
            "Search transactions in the local ledger, newest first. Amounts are signed: negative is money "
            "out, positive is money in; for purchases of 50 or more use max_amount -50. Pending entries "
            "come from bank alert emails; once a statement confirms one, only the posted transaction is "
            f"shown. Returns at most limit rows (200 maximum). Read-only. {_NO_MONEY_MOVEMENT}",
            Tier.READ,
            TRANSACTIONS_SCHEMA,
            transactions,
            title="Finance transactions",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "finance_summary",
            "Income and spending by category for one month (default: this month), with budget status. "
            "Categories configured as transfers between the user's own accounts are left out of the "
            "totals. Only the main currency is counted. Read-only.",
            Tier.READ,
            SUMMARY_SCHEMA,
            summary,
            title="Monthly finance summary",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "finance_recurring",
            "Detect subscriptions and other repeating charges: at least three charges from one merchant, "
            "monthly (30 +/- 5 days) or weekly (7 +/- 2 days), amounts within 10%. Shows cadence, typical "
            "amount, last date, and next expected date. A heuristic over the local ledger; read-only.",
            Tier.READ,
            RECURRING_SCHEMA,
            recurring,
            title="Recurring charges",
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "finance_import_inbox",
            "Import new OFX, QFX, and CSV statement files that the user saved in the configured inbox "
            "folder into the local ledger, and report counts per file. Files already imported are skipped. "
            f"Emits at most {MAX_EVENTS_PER_IMPORT} finance.transaction events per call. It cannot read "
            f"any other path or contact a bank. {_NO_MONEY_MOVEMENT}",
            Tier.DRAFT,
            _EMPTY,
            import_inbox,
            title="Import statement files",
            connector=NAME,
        )
    )


def transaction_query(args: Mapping[str, Any]) -> TransactionQuery:
    """Turn validated ``finance_transactions`` arguments into a ledger query."""
    start = _date(args.get("start"), "start")
    end = _date(args.get("end"), "end")
    if start is not None and end is not None and start > end:
        raise ToolError("start must not be after end")
    pending = args.get("pending")
    return TransactionQuery(
        start=start,
        end=end,
        account=args.get("account"),
        category=args.get("category"),
        merchant_contains=args.get("merchant_contains"),
        min_cents=_cents(args.get("min_amount"), "min_amount"),
        max_cents=_cents(args.get("max_amount"), "max_amount"),
        pending=None if pending is None else bool(pending),
        limit=int(args.get("limit", DEFAULT_LIMIT)),
    )


def _date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError(f"{label} must be a real date in the form YYYY-MM-DD") from exc


def _cents(value: Any, label: str) -> int | None:
    if value is None:
        return None
    try:
        return cents_from_number(value)
    except (ValueError, ArithmeticError) as exc:
        raise ToolError(f"{label} must be a finite number") from exc
