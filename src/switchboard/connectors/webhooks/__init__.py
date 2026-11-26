"""Webhooks: receive verified HTTP deliveries and turn them into ``webhook.received`` events.

Run the receiver with ``switchboard webhooks serve`` (127.0.0.1:8787 by default) and keep
``switchboard run`` going to process the events. See docs/connectors/webhooks.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from switchboard.connectors.webhooks.hooks import load_settings
from switchboard.connectors.webhooks.receiver import EVENT_TYPE, WebhookReceiver
from switchboard.connectors.webhooks.server import WebhookServer
from switchboard.context import AppContext
from switchboard.db import Migration
from switchboard.errors import ConfigError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from switchboard.app import App

NAME = "webhooks"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE webhooks_seen (
                hook TEXT NOT NULL,
                replay_key TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (hook, replay_key)
            )
            """,
            "CREATE INDEX webhooks_seen_expiry ON webhooks_seen(expires_at)",
        ),
    ),
]

RECENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    "additionalProperties": False,
}


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    load_settings(ctx.connector_config(NAME))  # refuse a malformed table early; no secrets resolved
    registry.add(
        Tool(
            name="webhooks_recent",
            description=(
                "List the most recent verified webhook deliveries: hook name, time received, and size "
                "in bytes. It never shows bodies or headers."
            ),
            tier=Tier.READ,
            input_schema=RECENT_SCHEMA,
            handler=_recent(ctx),
            title="Recent webhooks",
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    try:
        settings = load_settings(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    if not settings.hooks:
        return ["no hooks configured under [connectors.webhooks.hooks]"]
    return WebhookReceiver(ctx, settings, source=NAME).load_secrets()


def add_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("webhooks", help="receive verified webhooks over HTTP")
    actions = parser.add_subparsers(dest="webhooks_command", metavar="<action>", required=True)
    serve = actions.add_parser("serve", help="run the receiver until Ctrl+C")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"address to bind (default: {DEFAULT_HOST})")
    serve.add_argument(
        "--port", type=_port, default=DEFAULT_PORT, help=f"port to bind (default: {DEFAULT_PORT})"
    )
    serve.set_defaults(app_handler=_serve)


def _port(value: str) -> int:
    # socket.bind raises OverflowError, not OSError, for a port outside this range.
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number from 0 to 65535")
    return port


def _serve(args: argparse.Namespace, app: App) -> int:
    settings = load_settings(app.ctx.connector_config(NAME))
    if not settings.hooks:
        print("no hooks configured under [connectors.webhooks.hooks]", file=sys.stderr)
        return 1
    receiver = WebhookReceiver(app.ctx, settings, source=NAME)
    problems = receiver.load_secrets()
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if problems:
        return 1
    try:
        server = WebhookServer((args.host, args.port), receiver)
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    if args.host not in _LOOPBACK:
        print(
            f"warning: listening on {args.host} exposes the receiver beyond this machine; "
            "prefer 127.0.0.1 behind a tunnel you control",
            file=sys.stderr,
        )
    print(f"listening on http://{args.host}:{server.server_port}/hooks/<name>", file=sys.stderr)
    print(f"hooks: {', '.join(settings.hooks)}; run 'switchboard run' to process events", file=sys.stderr)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        server.server_close()
    return 0


def _recent(ctx: AppContext) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def recent(args: dict[str, Any]) -> dict[str, Any]:
        with ctx.db.session() as conn:
            rows = conn.execute(
                "SELECT id, payload_json, created_at FROM events "
                "WHERE source = ? AND type = ? ORDER BY id DESC LIMIT ?",
                (NAME, EVENT_TYPE, int(args.get("limit", 10))),
            ).fetchall()
        deliveries = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            deliveries.append(
                {
                    "event_id": row["id"],
                    "hook": payload.get("hook"),
                    "received_at": payload.get("received_at", row["created_at"]),
                    "size": payload.get("size"),
                }
            )
        return {"deliveries": deliveries}

    return recent
