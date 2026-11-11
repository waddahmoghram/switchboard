"""Telegram transport to an assistant-readable inbox; this module never starts an assistant model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeGuard

from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.util import canonical_json, iso, now, truncate

from .client import BotClient, TelegramError
from .settings import MAX_ID, Settings
from .store import Inbox


class TelegramService:
    def __init__(self, ctx: AppContext, settings: Settings, *, clock: Callable[[], datetime] = now) -> None:
        self.ctx = ctx
        self.settings = settings
        self.clock = clock
        self.client = BotClient(ctx, settings.bot_token)
        self.inbox = Inbox(ctx)

    def poll_job(self) -> None:
        self.poll()

    def prune_job(self) -> None:
        self.inbox.prune(self.clock(), self.settings.retention_days)

    def poll(self) -> dict[str, Any]:
        moment = self.clock()
        try:
            report = self._poll(moment)
        except ToolError as exc:
            self.ctx.db.kv_set(
                "telegram", "poll", canonical_json({"checked_at": iso(moment), "error": str(exc)})
            )
            raise
        self.ctx.db.kv_set("telegram", "poll", canonical_json(report))
        return report

    def _poll(self, moment: datetime) -> dict[str, Any]:
        session = self.client.open()
        if session.bot_id is None:
            raise ToolError("Telegram bot identity was not established")
        offset = self.inbox.offset(session.bot_id, moment)
        payload: dict[str, Any] = {
            "limit": 20,
            "timeout": self.settings.long_poll_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        updates = session.call("getUpdates", payload, timeout=self.settings.long_poll_seconds + 10)
        if not isinstance(updates, list) or len(updates) > 100:
            raise ToolError("Telegram returned an invalid update batch; the cursor was not advanced")
        messages: list[dict[str, Any]] = []
        identifiers: list[int] = []
        ignored = 0
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not _integer(update_id, positive=False) or update_id < 0:
                raise ToolError("Telegram returned an invalid update identifier; the cursor was not advanced")
            identifiers.append(update_id)
            message = normalize_message(update, self.settings)
            if message is None:
                ignored += 1
            else:
                messages.append(message)
        inserted = self.inbox.receive(
            session.bot_id, messages, max(identifiers) + 1 if identifiers else None, moment
        )
        self.inbox.prune(moment, self.settings.retention_days)
        return {
            "checked_at": iso(moment),
            "bot_id": session.bot_id,
            "received": len(updates),
            "inserted": inserted,
            "ignored": ignored,
            "error": None,
        }

    def identity(self) -> int:
        identifier = self.client.known_id()
        if identifier is None:
            raise ToolError("the bot is not identified yet; start 'switchboard run' to receive messages")
        return identifier

    def status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "bot_id": self.client.known_id(),
            "poll": self.inbox.poll_state(),
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "allow_groups": self.settings.allow_groups,
            "allowed_user_ids": sorted(self.settings.allowed_user_ids),
            "allowed_chat_ids": sorted(self.settings.allowed_chat_ids),
            "assistant_started": False,
        }

    def messages(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.inbox.messages(
            self.identity(),
            self.settings,
            unread=args.get("unread", True),
            limit=args.get("limit", 20),
            after_id=args.get("after_id", 0),
        )
        state = self.inbox.poll_state()
        result["poll"] = state
        result["warnings"] = [state["error"]] if state.get("error") else []
        return result

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.inbox.read(args["id"], self.identity(), self.settings)

    def mark_read(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.inbox.mark_read(args["id"], self.identity(), self.settings)

    def reply(self, args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"]
        if not text.strip():
            raise ToolError("reply text must not be blank")
        session = self.client.open()
        if session.bot_id is None:
            raise ToolError("Telegram bot identity was not established")
        message = self.inbox.read(args["id"], session.bot_id, self.settings)
        if message["chat_id"] != args["chat_id"]:
            raise ToolError("the approved chat does not match the stored incoming message")
        if not self.inbox.claim_reply(args["id"], text):
            raise ToolError(
                "this message has a sent, in-flight, or uncertain reply; inspect it before sending again"
            )
        try:
            result = session.call(
                "sendMessage",
                {
                    "chat_id": message["chat_id"],
                    "text": text,
                    "reply_parameters": {
                        "message_id": message["message_id"],
                        "allow_sending_without_reply": False,
                    },
                    "link_preview_options": {"is_disabled": True},
                },
            )
        except TelegramError as exc:
            self.inbox.finish_reply(args["id"], "unknown" if exc.ambiguous else "failed", error=str(exc))
            raise
        identifier = result.get("message_id") if isinstance(result, dict) else None
        chat = result.get("chat") if isinstance(result, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not _integer(identifier) or not _integer(chat_id, positive=False) or chat_id != message["chat_id"]:
            reason = "Telegram did not provide a matching reply confirmation; do not retry automatically"
            self.inbox.finish_reply(args["id"], "unknown", error=reason)
            raise ToolError(reason)
        self.inbox.finish_reply(args["id"], "sent", message_id=identifier)
        return {"id": args["id"], "chat_id": message["chat_id"], "status": "sent", "message_id": identifier}


def _integer(value: Any, *, positive: bool = True) -> TypeGuard[int]:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) <= MAX_ID
        and (value > 0 if positive else True)
    )


def normalize_message(update: Mapping[str, Any], settings: Settings) -> dict[str, Any] | None:
    """Unsupported kinds and unapproved senders are counted but never stored or delivered to an assistant."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender, chat = message.get("from"), message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict) or sender.get("is_bot") is not False:
        return None
    user_id, chat_id, message_id = sender.get("id"), chat.get("id"), message.get("message_id")
    if (
        not _integer(user_id)
        or not _integer(chat_id, positive=False)
        or chat_id == 0
        or not _integer(message_id)
    ):
        return None
    kind = chat.get("type")
    if not isinstance(kind, str) or not settings.allows(user_id, chat_id, kind):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        text = message.get("caption")
    stamp = message.get("date")
    if not isinstance(text, str) or not text or not _integer(stamp):
        return None
    try:
        sent_at = iso(datetime.fromtimestamp(stamp, UTC))
    except (ValueError, OverflowError, OSError):
        return None
    return {
        "update_id": update["update_id"],
        "chat_id": chat_id,
        "user_id": user_id,
        "message_id": message_id,
        "chat_type": kind,
        "text": truncate(text, 20_000),
        "truncated": len(text) > 20_000,
        "message_date": sent_at,
    }
