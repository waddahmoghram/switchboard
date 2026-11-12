"""Notify: short notes to your own phone, chat, or inbox, and never to anyone else.

Channels live in ``[connectors.notify.channels.<name>]`` (types ``ntfy``, ``telegram``, ``email``).
The connector provides the ``notify`` service and the ``notify_self`` tool. See docs/connectors/notify.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from switchboard.connectors.notify.channels import load_channels
from switchboard.connectors.notify.service import (
    MAX_TITLE,
    PRIORITIES,
    NotifyService,
    is_https_url,
    make_notification,
)
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError, UncertainOutcomeError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import truncate

NAME = "notify"

NOTIFY_SELF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "url": {
            "type": "string",
            "maxLength": 2000,
            "pattern": "^https://[!-~]+$",
            "description": "Optional https link the notification opens.",
        },
        "channels": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
            "minItems": 1,
            "maxItems": 10,
            "description": "Names of configured channels; omit to use all of them.",
        },
    },
    "required": ["title", "message"],
    "additionalProperties": False,
}

NOTIFY_SELF_DESCRIPTION = (
    "Send a short notification to the user's own devices through the channels they configured "
    "(ntfy push, their own Telegram chat, or their own inbox). There is no recipient argument: "
    "it cannot reach anyone else. Links must be https."
)


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    service = NotifyService(ctx, load_channels(ctx.connector_config(NAME)))
    ctx.provide("notify", service)
    registry.add(
        Tool(
            name="notify_self",
            description=NOTIFY_SELF_DESCRIPTION,
            tier=Tier.DRAFT,
            input_schema=NOTIFY_SELF_SCHEMA,
            handler=_notify_self(service),
            title="Notify me",
            summarize=_summarize,
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    try:
        channels = load_channels(ctx.connector_config(NAME))
    except ConfigError as exc:
        return [str(exc)]
    if not channels:
        return ["no channels configured under [connectors.notify.channels]"]
    return [
        f"channel {name}: {problem}" for name, channel in channels.items() for problem in channel.check(ctx)
    ]


def _notify_self(service: NotifyService) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        if not service.channels:
            raise ToolError("no notification channels are configured")
        names = args.get("channels")
        if names is not None:
            unknown = sorted(set(names) - set(service.channels))
            if unknown:
                raise ToolError(
                    f"unknown channel(s): {', '.join(unknown)}; configured: {', '.join(service.channels)}"
                )
        url = args.get("url")
        if url is not None and not is_https_url(url):
            raise ToolError("url must be a single https:// address without spaces")
        note = make_notification(
            args["title"], args["message"], priority=args.get("priority", "default"), url=url
        )
        report = service.deliver(note, names)
        if not report.sent:
            problems = {**report.failed, **report.uncertain}
            reasons = "; ".join(f"{name}: {reason}" for name, reason in problems.items())
            if report.uncertain:
                raise UncertainOutcomeError(f"no channel confirmed the notification ({reasons})")
            raise ToolError(f"no channel accepted the notification ({reasons})")
        return report.to_dict()

    return handler


def _summarize(args: Mapping[str, Any]) -> str:
    return f"notify yourself: {truncate(str(args.get('title', '')), 80)}"
