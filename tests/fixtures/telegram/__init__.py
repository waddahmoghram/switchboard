"""Synthetic bot updates and responses; no real user, chat, or credential."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tests.fakes import FakeHttp

TOKEN = "123456:synthetic-token-for-offline-tests"
REFERENCE = "env:SB_TEST_TELEGRAM_BOT"
BOT_ID = 777
USER_ID = 111
CHAT_ID = 111
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
BASE = f"https://api.telegram.org/bot{TOKEN}/"


def config(**overrides: Any) -> dict[str, Any]:
    settings = {
        "bot_token": REFERENCE,
        "allowed_user_ids": [USER_ID],
        "allowed_chat_ids": [CHAT_ID],
        "long_poll_seconds": 0,
    }
    settings.update(overrides)
    return {"switchboard": {"connectors": ["telegram"]}, "connectors": {"telegram": settings}}


def update(
    *,
    update_id: int = 100,
    message_id: int = 10,
    user: int = USER_ID,
    chat: int = CHAT_ID,
    chat_type: str = "private",
    text: str = "Please summarize my calendar.",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(NOW.timestamp()),
            "from": {"id": user, "is_bot": False, "first_name": "Example"},
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


def respond(http: FakeHttp, method: str, result: Any, *, status: int = 200) -> None:
    http.add("POST", BASE + method, status=status, body=json.dumps({"ok": True, "result": result}))


def identity(http: FakeHttp, bot_id: int = BOT_ID) -> None:
    respond(http, "getMe", {"id": bot_id, "is_bot": True, "username": "example_bot"})
