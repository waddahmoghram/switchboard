"""Telegram cursor, inbox, and single-reply claims. Message insertion and event enqueueing are atomic."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import iso, json_loads, now_iso, sha256_hex, truncate

from .settings import Settings

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            "CREATE TABLE telegram_cursors (bot_id INTEGER PRIMARY KEY, next_offset INTEGER NOT NULL, "
            "last_update_at TEXT NOT NULL)",
            "CREATE TABLE telegram_messages (id INTEGER PRIMARY KEY, bot_id INTEGER NOT NULL, "
            "update_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, chat_type TEXT NOT NULL, text TEXT, truncated INTEGER NOT NULL, "
            "message_date TEXT NOT NULL, received_at TEXT NOT NULL, read_at TEXT, "
            "reply_state TEXT NOT NULL DEFAULT 'new', reply_hash TEXT, reply_message_id INTEGER, "
            "reply_error TEXT, UNIQUE(bot_id, chat_id, message_id))",
            "CREATE INDEX telegram_inbox ON telegram_messages(bot_id, read_at, id)",
        ),
    )
]


class Inbox:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def offset(self, bot_id: int, moment: datetime) -> int | None:
        with self.ctx.db.session() as conn:
            row = conn.execute("SELECT * FROM telegram_cursors WHERE bot_id = ?", (bot_id,)).fetchone()
        if row is None:
            return None
        try:
            updated = datetime.fromisoformat(row["last_update_at"])
            age = moment - updated
        except (TypeError, ValueError):
            raise ToolError("Telegram cursor timestamp is unreadable") from None
        if age >= timedelta(days=6):
            return None  # Telegram may restart update numbering randomly after a week without updates.
        return int(row["next_offset"])

    def receive(
        self, bot_id: int, messages: Sequence[dict[str, Any]], next_offset: int | None, moment: datetime
    ) -> int:
        inserted = 0
        stamp = iso(moment)
        with self.ctx.db.session() as conn, transaction(conn):
            for message in messages:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO telegram_messages "
                    "(bot_id, update_id, chat_id, message_id, user_id, chat_type, text, truncated, "
                    "message_date, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bot_id,
                        message["update_id"],
                        message["chat_id"],
                        message["message_id"],
                        message["user_id"],
                        message["chat_type"],
                        message["text"],
                        message["truncated"],
                        message["message_date"],
                        stamp,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                inserted += 1
                self.ctx.events.emit(
                    "telegram",
                    "telegram.message",
                    {
                        "id": cursor.lastrowid,
                        "chat_id": message["chat_id"],
                        "user_id": message["user_id"],
                        "message_id": message["message_id"],
                        "untrusted_content": True,
                    },
                    dedupe_key=f"telegram:{bot_id}:{message['chat_id']}:{message['message_id']}",
                    connection=conn,
                )
            if next_offset is not None:
                conn.execute(
                    "INSERT INTO telegram_cursors (bot_id, next_offset, last_update_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(bot_id) DO UPDATE SET next_offset=excluded.next_offset, "
                    "last_update_at=excluded.last_update_at",
                    (bot_id, next_offset, stamp),
                )
        return inserted

    def messages(
        self, bot_id: int, settings: Settings, *, unread: bool, limit: int, after_id: int = 0
    ) -> dict[str, Any]:
        users = sorted(settings.allowed_user_ids)
        chats = sorted(settings.allowed_chat_ids)
        types = ["private", "group", "supergroup"] if settings.allow_groups else ["private"]
        conditions = [
            "bot_id = ?",
            "text IS NOT NULL",
            "id > ?",
            f"user_id IN ({','.join('?' for _ in users)})",
            f"chat_id IN ({','.join('?' for _ in chats)})",
            f"chat_type IN ({','.join('?' for _ in types)})",
        ]
        if unread:
            conditions.append("read_at IS NULL")
        parameters: list[Any] = [bot_id, after_id, *users, *chats, *types, limit + 1]
        with self.ctx.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM telegram_messages WHERE "  # noqa: S608 - fixed clauses, bound placeholders
                + " AND ".join(conditions)
                + " ORDER BY id LIMIT ?",
                parameters,
            ).fetchall()
        result = []
        for item in rows[:limit]:
            row = dict(item)
            row["text"] = truncate(row["text"], 500)
            row.pop("reply_hash", None)
            row.update(untrusted_content=True, preview_only=True)
            result.append(row)
        return {
            "messages": result,
            "truncated": len(rows) > limit,
            "untrusted_content": True,
            "next_after_id": result[-1]["id"] if result else after_id,
        }

    def read(self, record_id: int, bot_id: int, settings: Settings) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_messages WHERE id = ? AND bot_id = ?", (record_id, bot_id)
            ).fetchone()
        if (
            row is None
            or row["text"] is None
            or not settings.allows(row["user_id"], row["chat_id"], row["chat_type"])
        ):
            raise ToolError("no accessible, unexpired Telegram message with that id")
        result = dict(row)
        result.pop("reply_hash", None)
        result["untrusted_content"] = True
        return result

    def mark_read(self, record_id: int, bot_id: int, settings: Settings) -> dict[str, Any]:
        self.read(record_id, bot_id, settings)
        with self.ctx.db.session() as conn:
            conn.execute(
                "UPDATE telegram_messages SET read_at = COALESCE(read_at, ?) WHERE id = ?",
                (now_iso(), record_id),
            )
        return {"id": record_id, "status": "read"}

    def claim_reply(self, record_id: int, text: str) -> bool:
        with self.ctx.db.session() as conn:
            cursor = conn.execute(
                "UPDATE telegram_messages SET reply_state='sending', reply_hash=?, reply_error=NULL "
                "WHERE id=? AND reply_state IN ('new', 'failed') AND text IS NOT NULL",
                (sha256_hex(text), record_id),
            )
        return cursor.rowcount == 1

    def finish_reply(
        self, record_id: int, state: str, *, message_id: int | None = None, error: str | None = None
    ) -> None:
        with self.ctx.db.session() as conn:
            conn.execute(
                "UPDATE telegram_messages SET reply_state=?, reply_message_id=?, reply_error=?, "
                "read_at=CASE WHEN ?='sent' THEN ? ELSE read_at END WHERE id=? AND reply_state='sending'",
                (state, message_id, error, state, now_iso(), record_id),
            )

    def poll_state(self) -> dict[str, Any]:
        raw = self.ctx.db.kv_get("telegram", "poll")
        return {} if raw is None else dict(json_loads(raw))

    def prune(self, moment: datetime, days: int) -> None:
        with self.ctx.db.session() as conn:
            conn.execute(
                "UPDATE telegram_messages SET text=NULL WHERE received_at < ? AND text IS NOT NULL",
                (iso(moment - timedelta(days=days)),),
            )
