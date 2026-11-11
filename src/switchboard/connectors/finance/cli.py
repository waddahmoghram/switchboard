"""``switchboard finance ...`` subcommands for the person at the keyboard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard.connectors.finance.money import cents_from_number, format_cents
from switchboard.connectors.finance.runtime import FinanceRuntime
from switchboard.policy import Origin

if TYPE_CHECKING:
    from switchboard.app import App


def add_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    finance = subparsers.add_parser("finance", help="import statements and summarize your local ledger")
    actions = finance.add_subparsers(dest="finance_command", metavar="<action>", required=True)

    importer = actions.add_parser("import", help="import one OFX, QFX, or CSV file (the file is not moved)")
    importer.add_argument("file", type=Path)
    importer.add_argument("--account", help="ledger account name for this file's transactions")
    importer.add_argument("--profile", help="CSV profile from [connectors.finance.csv_profiles]")
    importer.set_defaults(app_handler=cmd_import)

    summary = actions.add_parser("summary", help="income and spending by category for one month")
    summary.add_argument("--month", help="YYYY-MM (default: this month)")
    summary.set_defaults(app_handler=cmd_summary)

    actions.add_parser(
        "recategorize", help="apply the current [connectors.finance.categories] to every transaction"
    ).set_defaults(app_handler=cmd_recategorize)


def cmd_import(args: argparse.Namespace, app: App) -> int:
    runtime = FinanceRuntime(app.ctx)
    result = runtime.import_path(args.file, today=runtime.today(), account=args.account, profile=args.profile)
    app.ctx.audit.append("finance.import", origin=Origin.HUMAN.value, files=result["files"])
    for item in result["files"]:
        print(_describe_file(item))
    print(f"events emitted: {result['events_emitted']} (not emitted: {result['events_not_emitted']})")
    for alert in result["budget_alerts"]:
        print(
            f"budget exceeded: {alert['category']} {alert['spent']} of {alert['limit']} {alert['currency']}"
        )
    return 0 if all(item["status"] in {"imported", "duplicate"} for item in result["files"]) else 1


def cmd_summary(args: argparse.Namespace, app: App) -> int:
    arguments = {"month": args.month} if args.month else {}
    outcome = app.call("finance_summary", arguments, Origin.HUMAN)
    if outcome.status != "ok":
        print(f"error: {outcome.message}", file=sys.stderr)
        return 1
    print(render_summary(outcome.data))
    return 0


def cmd_recategorize(args: argparse.Namespace, app: App) -> int:
    changed = FinanceRuntime(app.ctx).recategorize()
    app.ctx.audit.append("finance.recategorize", origin=Origin.HUMAN.value, changed=changed)
    print(f"recategorized {changed} transaction(s)")
    return 0


def render_summary(data: dict[str, Any]) -> str:
    """A plain-text table of a ``finance_summary`` result."""
    currency = data["currency"]
    lines = [
        f"{data['month']} ({currency})",
        f"income {_money(data['income'])}   spending {_money(data['spending'])}   net {_money(data['net'])}",
        "",
        f"{'category':24} {'spending':>12} {'income':>12} {'budget':>12} {'remaining':>12}",
    ]
    for entry in data["categories"]:
        name = entry["category"] + (" (transfer)" if entry.get("transfer") else "")
        budget = _money(entry["budget"]) if "budget" in entry else ""
        remaining = _money(entry["remaining"]) if "remaining" in entry else ""
        lines.append(
            f"{name:24} {_money(entry['spending']):>12} {_money(entry['income']):>12} "
            f"{budget:>12} {remaining:>12}"
        )
    return "\n".join(lines)


def _describe_file(item: dict[str, Any]) -> str:
    if item["status"] != "imported":
        detail = f": {item['error']}" if item.get("error") else ""
        return f"{item['file']}: {item['status']}{detail}"
    counts = json.dumps(
        {key: item[key] for key in ("new", "duplicates", "matched_pending", "skipped_rows")},
        separators=(", ", ": "),
    )
    return f"{item['file']}: imported into {', '.join(item['accounts'])} {counts}"


def _money(value: float) -> str:
    return format_cents(cents_from_number(value))
