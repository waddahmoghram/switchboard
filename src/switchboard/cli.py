"""Command-line interface: ``switchboard <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

from switchboard import __version__, cli_broker, cli_data, mcp_server
from switchboard.actions import STATES, ActionStateError, normalize_note
from switchboard.app import App, build_app, load_connectors
from switchboard.config import default_config_path, ensure_config_dir, ensure_private_dir, load_config
from switchboard.errors import ConfigError, SwitchboardError
from switchboard.locking import fcntl
from switchboard.policy import Origin
from switchboard.util import json_loads, printable


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=os.environ.get("SWITCHBOARD_LOG", "WARNING").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    known, _ = pre.parse_known_args(arguments)
    # Operator commands and the broker shim run under accounts that must not read the configuration.
    modules = [] if cli_broker.configuration_free(arguments) else _connector_cli_modules(known.config)
    parser = build_parser(modules)
    args = parser.parse_args(arguments)
    try:
        if hasattr(args, "app_handler"):
            return int(args.app_handler(args, _app(args)))
        if hasattr(args, "handler"):
            return int(args.handler(args))
        parser.print_help()
        return 2
    except SwitchboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _connector_cli_modules(config_path: Path | None) -> list[ModuleType]:
    """Enabled connectors that add their own subcommands; none when there is no usable config yet."""
    try:
        config = load_config(config_path)
        modules = load_connectors(config.enabled_connectors())
    except SwitchboardError:
        return []
    return [module for module in modules.values() if callable(getattr(module, "add_cli", None))]


def _app(args: argparse.Namespace) -> App:
    return build_app(load_config(args.config))


def build_parser(connector_modules: list[ModuleType] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard",
        description="Self-hosted hooks and a policy gate for personal AI assistants.",
    )
    parser.add_argument(
        "--config", type=Path, help="configuration file (default: ~/.config/switchboard/config.toml)"
    )
    parser.add_argument("--version", action="version", version=f"switchboard {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    init = sub.add_parser("init", help="write a starter configuration and a private data directory")
    init.add_argument("--force", action="store_true", help="overwrite an existing configuration")
    init.set_defaults(handler=cmd_init)

    sub.add_parser(
        "doctor", help="check configuration, permissions, secrets, and the audit chain"
    ).set_defaults(app_handler=cmd_doctor)
    sub.add_parser("tools", help="list tools with their tier and default decision").set_defaults(
        app_handler=cmd_tools
    )

    call = sub.add_parser("call", help="run a tool yourself (ask becomes allow; deny still denies)")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON object of tool arguments")
    call.set_defaults(app_handler=cmd_call)

    approvals = sub.add_parser("approvals", help="review actions waiting for your decision")
    approval_sub = approvals.add_subparsers(dest="approvals_command", metavar="<action>", required=True)
    listing = approval_sub.add_parser("list", help="pending approvals (or all with --all)")
    listing.add_argument("--all", action="store_true")
    listing.set_defaults(app_handler=cmd_approvals_list)
    show = approval_sub.add_parser("show", help="show one approval with its exact arguments")
    show.add_argument("id")
    show.set_defaults(app_handler=cmd_approvals_show)
    approve = approval_sub.add_parser("approve", help="approve and execute the stored arguments")
    approve.add_argument("id")
    approve.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    approve.set_defaults(app_handler=cmd_approvals_approve)
    deny = approval_sub.add_parser("deny", help="refuse an approval")
    deny.add_argument("id")
    deny.set_defaults(app_handler=cmd_approvals_deny)

    actions = sub.add_parser("actions", help="inspect recorded actions and settle unknown outcomes")
    action_sub = actions.add_subparsers(dest="actions_command", metavar="<action>", required=True)
    recent = action_sub.add_parser("list", help="recent actions, newest first")
    recent.add_argument("--state", choices=STATES, help="only actions in this state")
    recent.add_argument("-n", type=int, default=20, help="how many to show (1 to 500)")
    recent.set_defaults(app_handler=cmd_actions_list)
    detail = action_sub.add_parser("show", help="show one action with its stored arguments")
    detail.add_argument("id")
    detail.set_defaults(app_handler=cmd_actions_show)
    resolve = action_sub.add_parser(
        "resolve", help="record whether an uncertain action's effect happened (nothing is re-sent)"
    )
    resolve.add_argument("id")
    verdict = resolve.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--applied", action="store_true", help="you checked and the effect happened")
    verdict.add_argument(
        "--not-applied", action="store_true", help="you checked and the effect did not happen"
    )
    resolve.add_argument("--note", required=True, help="what you checked (1 to 500 printable characters)")
    resolve.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    resolve.set_defaults(app_handler=cmd_actions_resolve)

    audit = sub.add_parser("audit", help="inspect the hash-chained audit log")
    audit_sub = audit.add_subparsers(dest="audit_command", metavar="<action>", required=True)
    audit_sub.add_parser("verify", help="recompute the hash chain").set_defaults(app_handler=cmd_audit_verify)
    tail = audit_sub.add_parser("tail", help="print the latest records")
    tail.add_argument("-n", type=int, default=20)
    tail.set_defaults(app_handler=cmd_audit_tail)

    kill = sub.add_parser("kill-switch", help="deny every non-read action immediately")
    kill.add_argument("state", choices=["on", "off", "status"])
    kill.set_defaults(app_handler=cmd_kill_switch)

    events = sub.add_parser("events", help="show recent events")
    events.add_argument("-n", type=int, default=20)
    events.add_argument("--failed", action="store_true", help="show processing errors without event bodies")
    events.set_defaults(app_handler=cmd_events)

    serve_mcp = sub.add_parser("serve-mcp", help="run the MCP server on standard input and output")
    serve_mcp.add_argument(
        "--broker",
        type=Path,
        metavar="SOCKET",
        help="relay to a broker's client socket instead; reads no configuration or secrets",
    )
    serve_mcp.set_defaults(handler=cmd_serve_mcp)
    run = sub.add_parser("run", help="poll connectors, process events, and run rules until SIGINT or SIGTERM")
    run.add_argument(
        "--once",
        action="store_true",
        help="run every job once, wait for it within its deadline, process events, and exit",
    )
    run.set_defaults(app_handler=cmd_run)

    cli_data.add_commands(sub)
    cli_broker.add_commands(sub)
    for module in connector_modules or []:
        module.add_cli(sub)
    return parser


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    """Serve the tools directly, or relay to a broker without building the application."""
    if args.broker is not None:
        return cli_broker.serve_mcp_via_broker(args.broker)
    return mcp_server.run(_app(args))


def cmd_init(args: argparse.Namespace) -> int:
    path = (args.config or default_config_path()).expanduser()
    if path.is_symlink():
        raise ConfigError(f"configuration {path} must not be a symbolic link")
    if path.exists() and not args.force:
        print(f"{path} already exists; use --force to overwrite", file=sys.stderr)
        return 1
    ensure_config_dir(path.parent)
    starter = resources.files("switchboard").joinpath("config.example.toml").read_text(encoding="utf-8")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(starter)
            handle.flush()
            os.fsync(handle.fileno())
        if args.force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic no-overwrite publication, including a concurrent init.
    except FileExistsError:
        print(f"{path} already exists; use --force to overwrite", file=sys.stderr)
        return 1
    except OSError as exc:
        raise ConfigError(f"cannot write configuration {path}: {exc.strerror}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    config = load_config(path)
    ensure_private_dir(config.data_dir)
    print(f"wrote {path} (mode 600)")
    print(f"private data directory: {config.data_dir}")
    print("next: edit the file, enable connectors, then run 'switchboard doctor'")
    return 0


def cmd_doctor(args: argparse.Namespace, app: App) -> int:
    problems: list[str] = []
    path = app.ctx.config.path
    if path is not None and path.stat().st_mode & 0o077:
        problems.append(f"{path} is readable by others; run: chmod 600 {path}")
    for name, module in app.connectors.items():
        check = getattr(module, "doctor", None)
        if callable(check):
            problems.extend(f"{name}: {item}" for item in check(app.ctx))
    chain = app.ctx.audit.verify()
    if not chain.ok:
        problems.append(f"audit log: {chain.problem}")
    print(f"connectors: {', '.join(sorted(app.connectors)) or 'none'}")
    print(f"tools: {len(app.registry.all())}; audit records: {chain.records}")
    print(f"kill switch: {'ON' if app.policy.kill_switch.exists() else 'off'}")
    counts = app.journal.counts()
    print(f"actions: {counts['uncertain']} uncertain, {counts['in_flight']} in flight")
    if counts["uncertain"]:
        print("  review: 'switchboard actions list --state uncertain', then 'switchboard actions resolve'")
    for problem in problems:
        print(f"problem: {problem}")
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def cmd_tools(args: argparse.Namespace, app: App) -> int:
    for tool in app.registry.all():
        decision = app.policy.nominal(tool)
        print(f"{tool.name:32} {tool.tier.value:6} {decision.value:6} {tool.description}")
    return 0


def cmd_call(args: argparse.Namespace, app: App) -> int:
    try:
        payload = json_loads(args.args)
    except ValueError as exc:
        print(f"--args is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("--args must be a JSON object", file=sys.stderr)
        return 2
    outcome = app.call(args.tool, payload, Origin.HUMAN)
    print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=True, default=str))
    return 0 if outcome.status in {"ok", "pending"} else 1


def cmd_approvals_list(args: argparse.Namespace, app: App) -> int:
    app.approvals.expire()
    requests = app.approvals.list(None if args.all else "pending")
    if not requests:
        print("no approvals" if args.all else "nothing is waiting for you")
    for request in requests:
        print(
            f"{request.id}  {request.status:8} {printable(request.tool):24} "
            f"expires {request.expires_at}  {printable(request.summary)}"
        )
    return 0


def cmd_approvals_show(args: argparse.Namespace, app: App) -> int:
    request = app.approvals.get(args.id)
    if request is None:
        print(f"no approval {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(request.to_dict(), indent=2, ensure_ascii=True, default=str))
    return 0


def cmd_approvals_approve(args: argparse.Namespace, app: App) -> int:
    request = app.approvals.get(args.id)
    if request is None:
        print(f"no approval {args.id}", file=sys.stderr)
        return 1
    print(f"{printable(request.tool)}: {printable(request.summary)}")
    print(json.dumps(request.args, indent=2, ensure_ascii=True))
    if not args.yes:
        if not sys.stdin.isatty():
            print("refusing to approve without a terminal; pass --yes to confirm explicitly", file=sys.stderr)
            return 1
        if input("Approve exactly this? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("not approved")
            return 1
    outcome = app.dispatcher.approve(args.id)
    print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=True, default=str))
    return 0 if outcome.status == "ok" else 1


def cmd_approvals_deny(args: argparse.Namespace, app: App) -> int:
    if app.dispatcher.deny(args.id):
        print(f"denied {args.id}")
        return 0
    print(f"{args.id} is not pending", file=sys.stderr)
    return 1


def cmd_actions_list(args: argparse.Namespace, app: App) -> int:
    if not 1 <= args.n <= 500:
        print("-n must be between 1 and 500", file=sys.stderr)
        return 2
    records = app.journal.recent(state=args.state, limit=args.n)
    if not records:
        print("no recorded actions" if args.state is None else f"no {args.state} actions")
    for record in records:
        detail = record.resolution_note or record.error or ""
        print(
            f"{record.id}  {record.state:22} {printable(record.tool):24} {record.origin:9} "
            f"started {record.started_at}  {printable(detail)}"
        )
    return 0


def cmd_actions_show(args: argparse.Namespace, app: App) -> int:
    record = app.journal.get(args.id)
    if record is None:
        print(f"no action {printable(args.id)}", file=sys.stderr)
        return 1
    print(json.dumps(record.to_dict(), indent=2, ensure_ascii=True, default=str))
    return 0


def cmd_actions_resolve(args: argparse.Namespace, app: App) -> int:
    """Record the operator's finding for an uncertain action; this never repeats or reverses the effect."""
    try:
        note = normalize_note(args.note)
    except ActionStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    record = app.journal.get(args.id)
    if record is None:
        print(f"no action {printable(args.id)}", file=sys.stderr)
        return 1
    if record.state != "uncertain":
        print(f"action {record.id} is {record.state}; only uncertain actions are reconciled", file=sys.stderr)
        return 1
    applied = bool(args.applied)
    print(f"{printable(record.tool)} from {record.origin}, started {record.started_at}")
    print(f"recorded problem: {printable(record.error or 'none')}")
    print(json.dumps(record.args, indent=2, ensure_ascii=True))
    finding = "DID happen" if applied else "did NOT happen"
    print(f"You are recording that this effect {finding}. Switchboard will not repeat, undo, or check it.")
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "refusing to reconcile without a terminal; pass --yes to confirm explicitly", file=sys.stderr
            )
            return 1
        if input("Record exactly this? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("nothing recorded")
            return 1
    try:
        resolved = app.journal.reconcile(args.id, applied=applied, note=note)
    except ActionStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    app.ctx.audit.append(
        "action.reconciled",
        action=resolved.id,
        tool=resolved.tool,
        state=resolved.state,
        note=resolved.resolution_note,
    )
    print(f"{resolved.id} is now {resolved.state}; the duplicate guard no longer blocks an identical request")
    return 0


def cmd_audit_verify(args: argparse.Namespace, app: App) -> int:
    result = app.ctx.audit.verify()
    if result.ok:
        print(f"ok: {result.records} records, head {result.head}")
        return 0
    print(f"BROKEN: {result.problem}", file=sys.stderr)
    return 1


def cmd_audit_tail(args: argparse.Namespace, app: App) -> int:
    for record in app.ctx.audit.tail(args.n):
        print(json.dumps(record, ensure_ascii=True))
    return 0


def cmd_kill_switch(args: argparse.Namespace, app: App) -> int:
    switch = app.policy.kill_switch
    if args.state == "on":
        switch.touch(mode=0o600, exist_ok=True)
        app.ctx.audit.append("kill_switch.on")
    elif args.state == "off" and switch.exists():
        switch.unlink()
        app.ctx.audit.append("kill_switch.off")
    print(f"kill switch: {'ON' if switch.exists() else 'off'}")
    return 0


def cmd_events(args: argparse.Namespace, app: App) -> int:
    if args.failed:
        for failure in app.ctx.events.failures(args.n):
            print(json.dumps(failure, ensure_ascii=True))
        return 0
    for event in app.ctx.events.recent(args.n):
        keys = ", ".join(printable(key) for key in sorted(event.payload)[:8])
        kind = printable(event.type)
        print(f"{event.id:6} {event.created_at} {kind:28} from {printable(event.source)}: {keys}")
    return 0


@contextmanager
def runner_lock(data_dir: Path) -> Iterator[bool]:
    """Hold ``runner.lock`` so only one process polls and processes events; yields False if taken."""
    fd = os.open(data_dir / "runner.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def cmd_run(args: argparse.Namespace, app: App) -> int:
    with runner_lock(app.ctx.data_dir) as acquired:
        if not acquired:
            print("another 'switchboard run' is already processing events", file=sys.stderr)
            return 1
        return _run(args, app)


def _run(args: argparse.Namespace, app: App) -> int:
    scheduler = app.ctx.scheduler
    if args.once:
        return _run_once(app)
    print(
        f"running {len(scheduler.jobs())} job(s) on up to {scheduler.workers} worker(s); "
        "press Ctrl+C or send SIGTERM to stop",
        file=sys.stderr,
    )
    stop = RunnerStop()
    previous = _install_signal_handlers(stop)
    try:
        abandoned = serve(app, stop)
    finally:
        _restore_signal_handlers(previous)
    if abandoned:
        print("stopped; left running past their deadlines: " + ", ".join(abandoned), file=sys.stderr)
        return 1
    print("stopped", file=sys.stderr)
    return 0


def _run_once(app: App) -> int:
    """Run every job once, wait for them within their deadlines, process events, and report problems."""
    scheduler = app.ctx.scheduler
    app.journal.sweep()
    app.approvals.expire()
    ran = scheduler.run_all()
    handled = app.process_events()
    print(f"ran {len(ran)} job(s); processed {handled} event(s)")
    finished = {item["name"]: item for item in scheduler.status() if not item["running"]}
    failed = [name for name in ran if name in finished and finished[name]["last_error"] is not None]
    stuck = scheduler.stuck()
    skipped = scheduler.skipped()
    if failed:
        print("failed jobs: " + ", ".join(failed), file=sys.stderr)
    if stuck:
        print("stuck jobs (still running past their deadline): " + ", ".join(stuck), file=sys.stderr)
    if skipped:
        print(
            "jobs not started (their lane or every worker stayed busy): " + ", ".join(skipped),
            file=sys.stderr,
        )
    return 1 if failed or stuck or skipped else 0


class RunnerStop:
    """Signal handler state: the first SIGINT or SIGTERM asks for a graceful stop; a second one interrupts."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, _frame: FrameType | None) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True


def serve(app: App, stop: RunnerStop) -> list[str]:
    """Tick until a stop is requested, then stop scheduling and wait for running jobs within their deadlines.

    Returns the jobs still running (stuck past their deadline) when the wait ended. Events are processed only
    on this thread, between waits; a job finishing wakes the loop early.
    """
    scheduler = app.ctx.scheduler
    while not stop.requested:
        app.tick()
        resume = time.monotonic() + min(30.0, max(1.0, scheduler.seconds_until_next()))
        while not stop.requested:
            left = resume - time.monotonic()
            if left <= 0 or scheduler.wait(min(1.0, left)):
                break
    return scheduler.shutdown()


SignalHandler = Callable[[int, FrameType | None], Any] | int | None


def _install_signal_handlers(stop: RunnerStop) -> dict[int, SignalHandler]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}


def _restore_signal_handlers(previous: dict[int, SignalHandler]) -> None:
    for number, handler in previous.items():
        # None means a handler that was not installed from Python; the default is the closest equivalent.
        signal.signal(number, signal.SIG_DFL if handler is None else handler)
