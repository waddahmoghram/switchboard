"""A fixed-host bot client; token-bearing addresses and transport exceptions never leave this module."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError, ToolError
from switchboard.util import iso, now, sha256_hex

from .settings import MAX_ID

BASE = "https://api.telegram.org"
_TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
_METHODS = frozenset({"getMe", "getUpdates", "sendMessage"})


class TelegramError(ToolError):
    def __init__(self, message: str, *, ambiguous: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.status = status


@dataclass
class BotSession:
    ctx: AppContext = field(repr=False)
    token: str = field(repr=False)
    bot_id: int | None = None

    def call(self, method: str, payload: dict[str, Any], *, timeout: float = 20) -> Any:
        if method not in _METHODS:
            raise TelegramError("unsupported Telegram method")
        key = "retry:" + sha256_hex(self.token)
        retry = self.ctx.db.kv_get("telegram", key)
        if retry is not None:
            try:
                until = datetime.fromisoformat(retry)
                waiting = until > now()
            except (ValueError, TypeError):
                raise TelegramError("Telegram backoff state is unreadable") from None
            if waiting:
                raise TelegramError(f"Telegram requested a pause until {retry}")
        try:
            response = self.ctx.http.request(
                "POST",
                f"{BASE}/bot{self.token}/{method}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload, ensure_ascii=True).encode("ascii"),
                timeout=timeout,
                max_bytes=1_000_000,
                follow_redirects=False,
            )
        except HttpError:
            raise TelegramError("could not reach Telegram", ambiguous=method == "sendMessage") from None
        try:
            reply = response.json()
        except (ValueError, RecursionError):
            raise TelegramError(
                "Telegram returned an unreadable reply", ambiguous=method == "sendMessage"
            ) from None
        if response.ok and isinstance(reply, dict) and reply.get("ok") is True and "result" in reply:
            return reply["result"]
        hints = {
            400: "the request or reply target was refused",
            401: "check the bot credential",
            403: "the bot is not permitted to message this chat",
            409: "another poller or an active webhook conflicts with this receiver",
            429: "the bot is rate limited",
        }
        detail = ": " + hints[response.status] if response.status in hints else ""
        parameters = reply.get("parameters") if isinstance(reply, dict) else None
        seconds = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
            self.ctx.db.kv_set("telegram", key, iso(now() + timedelta(seconds=min(seconds, 86400))))
        raise TelegramError(
            f"Telegram answered HTTP {response.status}{detail}",
            ambiguous=method == "sendMessage" and not 400 <= response.status < 500,
            status=response.status,
        )


class BotClient:
    def __init__(self, ctx: AppContext, token_reference: str) -> None:
        self.ctx = ctx
        self.token_reference = token_reference

    def _token(self) -> str:
        try:
            token = self.ctx.secret(self.token_reference)
        except SecretError:
            raise TelegramError("bot_token could not be resolved; check the secret reference") from None
        if len(token) > 512 or _TOKEN.fullmatch(token) is None:
            raise TelegramError("bot_token must have the form digits:letters")
        return token

    def _known(self, token: str) -> int | None:
        raw = self.ctx.db.kv_get("telegram", "identity:" + sha256_hex(token))
        if raw is None:
            return None
        if re.fullmatch(r"[0-9]{1,16}", raw) is None or not 1 <= int(raw) <= MAX_ID:
            raise TelegramError("stored Telegram bot identity is unreadable")
        return int(raw)

    def known_id(self) -> int | None:
        return self._known(self._token())

    def open(self) -> BotSession:
        token = self._token()
        session = BotSession(self.ctx, token, self._known(token))
        if session.bot_id is None:
            identity = session.call("getMe", {})
            identifier = identity.get("id") if isinstance(identity, dict) else None
            if (
                not isinstance(identity, dict)
                or identity.get("is_bot") is not True
                or isinstance(identifier, bool)
                or not isinstance(identifier, int)
                or not 1 <= identifier <= MAX_ID
            ):
                raise TelegramError("Telegram returned an invalid bot identity")
            session.bot_id = identifier
            self.ctx.db.kv_set("telegram", "identity:" + sha256_hex(token), str(identifier))
        return session
