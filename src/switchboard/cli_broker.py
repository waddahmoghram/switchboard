"""Command-line wiring for the broker: ``broker serve``, ``operator ...``, and the shim behind
``serve-mcp --broker SOCKET``.

``broker serve`` runs as the service account and loads the application. ``operator`` commands and the shim run
under accounts that must not read the configuration or secrets, so they use ``set_defaults(handler=...)``,
which never builds the application. See ``docs/ISOLATION.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import broker
from switchboard.actions import STATES
from switchboard.util import printable

if TYPE_CHECKING:
    from switchboard.app import App


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``broker serve`` and the ``operator`` client."""
    service = subparsers.add_parser(
        "broker", help="own credentials and the database; serve the assistant and operator sockets"
    )
    service_actions = service.add_subparsers(dest="broker_command", metavar="<action>", required=True)
    serve = service_actions.add_parser(
        "serve", help="serve [broker] client_socket and operator_socket until SIGTERM or SIGINT"
    )
    serve.add_argument(
        "--client-group", type=_group, metavar="GROUP", help="group that owns the client socket (mode 660)"
    )
    serve.add_argument(
        "--operator-group",
        type=_group,
        metavar="GROUP",
        help="group that owns the operator socket (mode 660)",
    )
    serve.add_argument(
        "--allow-shared-account",
        action="store_true",
        help="serve even when one account may use both sockets; this gives no approval separation",
    )
    serve.set_defaults(app_handler=cmd_broker_serve)

    operator = subparsers.add_parser(
        "operator", help="decide approvals and reconcile actions through the broker's operator socket"
    )
    operator.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help=f"operator socket (default: ${broker.OPERATOR_SOCKET_ENV} or {broker.DEFAULT_OPERATOR_SOCKET})",
    )
    actions = operator.add_subparsers(dest="operator_command", metavar="<action>", required=True)
    actions.add_parser(
        "status", help="engine, kill switch, pending approvals, and open actions"
    ).set_defaults(handler=cmd_operator_status)

    approvals = actions.add_parser("approvals", help="review actions waiting for your decision")
    approval_actions = approvals.add_subparsers(dest="approvals_command", metavar="<action>", required=True)
    approval_actions.add_parser("list", help="pending approvals, without their arguments").set_defaults(
        handler=cmd_operator_approvals_list
    )
    show = approval_actions.add_parser("show", help="show one approval with its exact arguments")
    show.add_argument("id")
    show.set_defaults(handler=cmd_operator_approvals_show)
    approve = approval_actions.add_parser("approve", help="approve and execute exactly the shown arguments")
    approve.add_argument("id")
    approve.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    approve.set_defaults(handler=cmd_operator_approvals_approve)
    deny = approval_actions.add_parser("deny", help="refuse an approval")
    deny.add_argument("id")
    deny.set_defaults(handler=cmd_operator_approvals_deny)

    journal = actions.add_parser("actions", help="inspect and reconcile the action journal")
    journal_actions = journal.add_subparsers(dest="actions_command", metavar="<action>", required=True)
    recent = journal_actions.add_parser("list", help="recent journal entries without their arguments")
    recent.add_argument("--state", choices=STATES)
    recent.add_argument("-n", type=int, default=20, help="how many entries (1-100)")
    recent.set_defaults(handler=cmd_operator_actions_list)
    entry = journal_actions.add_parser("show", help="one journal entry with its arguments")
    entry.add_argument("id")
    entry.set_defaults(handler=cmd_operator_actions_show)
    resolve = journal_actions.add_parser(
        "resolve", help="record whether an uncertain effect happened; nothing is repeated or reversed"
    )
    resolve.add_argument("id")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--applied", action="store_true", help="the effect happened")
    outcome.add_argument("--not-applied", action="store_true", help="the effect did not happen")
    resolve.add_argument("--note", required=True, help="what you checked (1-500 printable characters)")
    resolve.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    resolve.set_defaults(handler=cmd_operator_actions_resolve)

    kill = actions.add_parser("kill-switch", help="deny every non-read action immediately")
    kill.add_argument("state", choices=["on", "off", "status"])
    kill.set_defaults(handler=cmd_operator_kill_switch)


def serve_mcp_via_broker(socket: Path) -> int:
    """``serve-mcp --broker SOCKET``: relay standard input and output to the broker's client socket."""
    return broker.run_shim(socket)


def configuration_free(argv: Sequence[str]) -> bool:
    """True for ``operator ...`` and ``serve-mcp --broker``, which must not read configuration.

    ``main`` can skip connector subcommand discovery (which loads the configuration) when this is true.
    """
    pre = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    pre.add_argument("--config")
    pre.add_argument("command", nargs="?")
    try:
        known, rest = pre.parse_known_args(list(argv))
    except argparse.ArgumentError:
        return False
    if known.command == "operator":
        return True
    return known.command == "serve-mcp" and any(
        item == "--broker" or item.startswith("--broker=") for item in rest
    )


def _group(value: str) -> int:
    if value.isdigit():
        return int(value)
    try:
        import grp
    except ImportError as exc:  # pragma: no cover - the broker needs a Unix system
        raise argparse.ArgumentTypeError("group names need a Unix system") from exc
    try:
        return grp.getgrnam(value).gr_gid
    except KeyError as exc:
        raise argparse.ArgumentTypeError(f"no group named {value!r}") from exc


def cmd_broker_serve(args: argparse.Namespace, app: App) -> int:
    settings = broker.broker_settings(
        app.ctx.config, client_group=args.client_group, operator_group=args.operator_group
    )
    problems = broker.separation_problems(settings, service_uid=os.getuid())
    label = "warning" if args.allow_shared_account else "refusing"
    for problem in problems:
        print(f"{label}: {problem}", file=sys.stderr)
    if problems and not args.allow_shared_account:
        print(
            "use separate accounts (docs/ISOLATION.md), or pass --allow-shared-account "
            "to run without separation",
            file=sys.stderr,
        )
        return 1
    return broker.serve(app, settings)


def operator_socket(args: argparse.Namespace) -> Path:
    if getattr(args, "socket", None) is not None:
        return Path(args.socket)
    configured = os.environ.get(broker.OPERATOR_SOCKET_ENV)
    return Path(configured) if configured else broker.DEFAULT_OPERATOR_SOCKET


def _client(args: argparse.Namespace) -> broker.OperatorClient:
    return broker.open_operator_client(operator_socket(args))


def _show_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True, default=str))


def _text(value: Any) -> str:
    return printable(str(value if value is not None else ""))


def _confirmed(prompt: str, args: argparse.Namespace) -> bool:
    """``--yes``, or an interactive yes; without a terminal nothing is confirmed implicitly."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("refusing to continue without a terminal; pass --yes to confirm explicitly", file=sys.stderr)
        return False
    if input(prompt).strip().lower() in {"y", "yes"}:
        return True
    print("nothing was recorded or approved")
    return False


def cmd_operator_status(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.call("status")
    _show_json(result)
    return _report(result)


def cmd_operator_approvals_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.call("approvals")
    if _report(result):
        return 1
    approvals = _rows(result, "data")
    if not approvals:
        print("nothing is waiting for you")
    for item in approvals:
        print(
            f"{_text(item.get('id'))}  {_text(item.get('tool')):24} "
            f"expires {_text(item.get('expires_at'))}  {_text(item.get('summary'))}"
        )
    return 0


def cmd_operator_approvals_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _show_json(client.call("approval", id=args.id))
    return 0


def cmd_operator_approvals_approve(args: argparse.Namespace) -> int:
    with _client(args) as client:
        approval = client.call("approval", id=args.id)
        if not isinstance(approval, dict) or not isinstance(approval.get("args"), dict):
            print("the broker did not return the approval's arguments; nothing was approved", file=sys.stderr)
            return 1
        print(f"{_text(approval.get('tool'))}: {_text(approval.get('summary'))}")
        print(json.dumps(approval["args"], indent=2, ensure_ascii=True, sort_keys=True))
        if approval.get("status") != "pending":
            print(
                f"approval {printable(args.id)} is {_text(approval.get('status'))}, not pending",
                file=sys.stderr,
            )
            return 1
        if not _confirmed("Approve exactly this? [y/N] ", args):
            return 1
        result = client.call("approve", id=args.id)
    _show_json(result)
    return 0 if isinstance(result, dict) and result.get("status") == "ok" else 1


def cmd_operator_approvals_deny(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.call("deny", id=args.id)
    if isinstance(result, dict) and result.get("status") == "denied":
        print(f"denied {printable(args.id)}")
        return 0
    print(
        f"not denied: {_text(result.get('message') if isinstance(result, dict) else result)}", file=sys.stderr
    )
    return 1


def cmd_operator_actions_list(args: argparse.Namespace) -> int:
    fields: dict[str, Any] = {"limit": args.n}
    if args.state is not None:
        fields["state"] = args.state
    with _client(args) as client:
        result = client.call("actions", **fields)
    if _report(result):
        return 1
    records = _rows(result, "actions")
    if not records:
        print("no journal entries")
    for item in records:
        detail = item.get("resolution_note") or item.get("error")
        print(
            f"{_text(item.get('id'))}  {_text(item.get('state')):22} {_text(item.get('tool')):24} "
            f"started {_text(item.get('started_at'))}  {_text(detail)}"
        )
    return 0


def cmd_operator_actions_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.call("actions", id=args.id)
    if _report(result):
        return 1
    _show_json(result.get("action"))
    return 0


def cmd_operator_actions_resolve(args: argparse.Namespace) -> int:
    resolution = "applied" if args.applied else "not_applied"
    with _client(args) as client:
        shown = client.call("actions", id=args.id)
        if _report(shown):
            return 1
        action = shown.get("action")
        if not isinstance(action, dict):
            print("the broker did not return the action; nothing was recorded", file=sys.stderr)
            return 1
        if action.get("state") != "uncertain":
            current = _text(action.get("state"))
            print(
                f"action {printable(args.id)} is {current}; only uncertain actions are reconciled",
                file=sys.stderr,
            )
            return 1
        print(
            f"{_text(action.get('tool'))} from {_text(action.get('origin'))}, "
            f"started {_text(action.get('started_at'))}"
        )
        print(f"recorded problem: {_text(action.get('error') or 'none')}")
        print(json.dumps(action.get("args"), indent=2, ensure_ascii=True, sort_keys=True))
        finding = "DID happen" if args.applied else "did NOT happen"
        print(
            f"You are recording that this effect {finding}. Switchboard will not repeat, undo, or check it."
        )
        if not _confirmed("Record exactly this? [y/N] ", args):
            return 1
        result = client.call("reconcile", id=args.id, resolution=resolution, note=args.note)
    if _report(result):
        return 1
    resolved = result.get("action")
    print(
        f"{printable(args.id)} is now {_text(resolved.get('state') if isinstance(resolved, dict) else None)}"
    )
    return 0


def cmd_operator_kill_switch(args: argparse.Namespace) -> int:
    fields: dict[str, Any] = {} if args.state == "status" else {"enable": args.state == "on"}
    with _client(args) as client:
        result = client.call("kill_switch", **fields)
    if _report(result):
        return 1
    print(f"kill switch: {'ON' if result.get('kill_switch') is True else 'off'}")
    return 0


def _rows(result: Any, key: str) -> list[dict[str, Any]]:
    rows = result.get(key) if isinstance(result, dict) else None
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _report(result: Any) -> int:
    """1 after printing why, when an operation ran but refused (its ``status`` is not ``ok``)."""
    if isinstance(result, dict) and result.get("status") == "ok":
        return 0
    message = result.get("message") if isinstance(result, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    print(f"{_text(status or 'unexpected reply')}: {_text(message or result)}", file=sys.stderr)
    return 1
