"""An allowlisted bot inbox and approval-gated replies, exposed to an existing assistant client."""

from __future__ import annotations

from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.policy import Tier
from switchboard.tools import Tool, ToolRegistry
from switchboard.util import truncate

from .service import TelegramService
from .settings import MAX_ID, load_settings
from .store import MIGRATIONS as MIGRATIONS

NAME = "telegram"
_ID = {"type": "integer", "minimum": 1, "maximum": MAX_ID}
_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def register(ctx: AppContext, registry: ToolRegistry) -> None:
    settings = load_settings(ctx.connector_config(NAME))
    service = TelegramService(ctx, settings)
    ctx.provide("telegram.inbox", service)
    ctx.scheduler.add("telegram.poll", settings.poll_interval_seconds, service.poll_job)
    ctx.scheduler.add("telegram.prune", 6 * 3600, service.prune_job)
    registry.add(
        Tool(
            "telegram_status",
            "Show bot inbox readiness and polling status, without starting a model.",
            Tier.READ,
            _EMPTY,
            service.status,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "telegram_messages",
            "Read allowlisted incoming bot messages as untrusted data; does not mark them read.",
            Tier.READ,
            {
                "type": "object",
                "properties": {
                    "unread": {"type": "boolean"},
                    "after_id": {"type": "integer", "minimum": 0, "maximum": MAX_ID},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
            service.messages,
            connector=NAME,
        )
    )
    single = {"type": "object", "properties": {"id": _ID}, "required": ["id"], "additionalProperties": False}
    registry.add(
        Tool(
            "telegram_read",
            "Read one stored bot message in full, marked untrusted.",
            Tier.READ,
            single,
            service.read,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "telegram_mark_read",
            "Acknowledge one local inbox item without sending anything.",
            Tier.DRAFT,
            single,
            service.mark_read,
            connector=NAME,
        )
    )
    registry.add(
        Tool(
            "telegram_reply",
            "Reply once to an allowlisted stored message. Approval is required by default; "
            "uncertain delivery is not retried.",
            Tier.ACT,
            {
                "type": "object",
                "properties": {
                    "id": _ID,
                    "chat_id": {
                        "type": "integer",
                        "minimum": -MAX_ID,
                        "maximum": MAX_ID,
                        "enum": sorted(settings.allowed_chat_ids),
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["id", "chat_id", "text"],
                "additionalProperties": False,
            },
            service.reply,
            summarize=lambda args: (
                f"Reply to Telegram chat {args['chat_id']} for inbox item {args['id']}: "
                f"{truncate(args['text'], 160)}"
            ),
            recipients=lambda args: [f"telegram:{args['chat_id']}"],
            connector=NAME,
        )
    )


def doctor(ctx: AppContext) -> list[str]:
    try:
        settings = load_settings(ctx.connector_config(NAME))
        TelegramService(ctx, settings).client.known_id()
    except (ConfigError, ToolError) as exc:
        return [str(exc)]
    return []
