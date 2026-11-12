"""Notification channels: ntfy, Telegram, and email to your own inbox.

A channel raises :class:`ChannelError` when it cannot deliver. Its message is safe to log and to show
an assistant: it never contains a secret, an ntfy topic, or a request URL. That matters because an ntfy
topic on a public server works like a password, and Telegram's Bot API puts the bot token in the path.
A channel raises :class:`ChannelUncertainError` instead when the note may have been delivered anyway.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from switchboard.context import AppContext
from switchboard.errors import ConfigError, HttpError, SecretError, SwitchboardError, UncertainOutcomeError
from switchboard.httpclient import HttpResponse
from switchboard.secrets import is_reference
from switchboard.util import truncate

NTFY_DEFAULT_SERVER = "https://ntfy.sh"
NTFY_MAX_BODY_BYTES = 4096  # ntfy turns longer messages into file attachments
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_MAX_TEXT = 4096
QUIET_PRIORITIES = frozenset({"min", "low"})
MAIL_MISSING = "needs the mail.sender service; enable the mail connector"

_CHANNEL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NTFY_TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
_NTFY_TAG = re.compile(r"[A-Za-z0-9_+-]{1,64}")
_TELEGRAM_BOT = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
_TELEGRAM_CHAT = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{5,64}")
_ADDRESS = re.compile(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+")
_HEADER_SAFE = re.compile(r"[!-~]+")


class ChannelError(Exception):
    """A channel could not deliver. The message is safe to log: no secrets, topics, or URLs."""


class ChannelUncertainError(ChannelError):
    """The note may have been delivered: the connection failed after sending began, or the service
    answered HTTP 408 or 5xx. It was not resent."""


@dataclass(frozen=True)
class Notification:
    """One note to deliver. Build it with ``make_notification`` so the text is normalized first."""

    title: str
    message: str
    priority: str = "default"
    url: str | None = None


@dataclass(frozen=True)
class NtfyChannel:
    """Publish to an ntfy topic: the body is the message; Title, Priority, Tags, and Click are headers."""

    name: str
    topic: str  # secret reference: on a public server the topic name works like a password
    server: str = NTFY_DEFAULT_SERVER
    token: str | None = None  # optional secret reference, sent as "Authorization: Bearer <token>"
    tags: tuple[str, ...] = ()
    allow_http: bool = False

    def send(self, note: Notification, ctx: AppContext) -> None:
        topic = _secret(ctx, self.topic, "topic")
        if not _NTFY_TOPIC.fullmatch(topic):
            raise ChannelError("the topic must be 1-64 letters, digits, '-' or '_'")
        response = _post(
            ctx,
            f"{self.server}/{topic}",
            headers=self._headers(note, ctx),
            body=fit_utf8(note.message, NTFY_MAX_BODY_BYTES),
            allow_http=self.allow_http,
            host=urllib.parse.urlsplit(self.server).hostname or "the ntfy server",
        )
        if not response.ok:
            message = f"ntfy answered HTTP {response.status}"
            if _ambiguous(response.status):
                raise ChannelUncertainError(f"{message}; the note may have been delivered and was not resent")
            raise ChannelError(message)

    def check(self, ctx: AppContext) -> list[str]:
        problems = _check_secret(ctx, self.topic, "topic", _NTFY_TOPIC)
        if self.token is not None:
            problems += _check_secret(ctx, self.token, "token", _HEADER_SAFE)
        return problems

    def _headers(self, note: Notification, ctx: AppContext) -> dict[str, str]:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": header_text(note.title),
            "Priority": note.priority,
        }
        if self.tags:
            headers["Tags"] = ",".join(self.tags)
        if note.url:
            headers["Click"] = note.url
        if self.token is not None:
            headers["Authorization"] = "Bearer " + _header_secret(ctx, self.token, "token")
        return headers


@dataclass(frozen=True)
class TelegramChannel:
    """Send through the Bot API's sendMessage. The token sits in the URL path, so URLs never leave here."""

    name: str
    bot_token: str  # secret reference
    chat_id: int | str

    def send(self, note: Notification, ctx: AppContext) -> None:
        bot = _secret(ctx, self.bot_token, "bot_token")
        if not _TELEGRAM_BOT.fullmatch(bot):
            raise ChannelError("the bot_token does not look like <digits>:<letters>")
        payload = {
            "chat_id": self.chat_id,
            "text": telegram_text(note),
            "disable_notification": note.priority in QUIET_PRIORITIES,
            "link_preview_options": {"is_disabled": True},
        }
        response = _post(
            ctx,
            f"{TELEGRAM_API}/bot{bot}/sendMessage",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode("ascii"),
            allow_http=False,
            host=TELEGRAM_HOST,
        )
        _ensure_telegram_ok(response, bot)

    def check(self, ctx: AppContext) -> list[str]:
        return _check_secret(ctx, self.bot_token, "bot_token", _TELEGRAM_BOT)


@dataclass(frozen=True)
class EmailChannel:
    """Mail yourself through the mail connector's ``mail.sender`` service, looked up at send time."""

    name: str
    to: tuple[str, ...]
    account: str | None = None

    def send(self, note: Notification, ctx: AppContext) -> None:
        sender = ctx.service("mail.sender")
        if sender is None:
            raise ChannelError(MAIL_MISSING)
        message = EmailMessage()
        message["To"] = ", ".join(self.to)
        message["Subject"] = note.title
        message.set_content(note.message + (f"\n\n{note.url}" if note.url else ""))
        try:
            sender.send(message, self.account)
        except UncertainOutcomeError as exc:
            raise ChannelUncertainError(f"the mail connector could not confirm sending: {exc}") from exc
        except SwitchboardError as exc:
            raise ChannelError(f"the mail connector could not send: {exc}") from exc

    def check(self, ctx: AppContext) -> list[str]:
        return [] if ctx.service("mail.sender") is not None else [MAIL_MISSING]


Channel = NtfyChannel | TelegramChannel | EmailChannel


def header_text(value: str) -> str:
    """Return one-line ``value`` ready for an HTTP header: ASCII as is, anything else RFC 2047 encoded.

    Python's HTTP client cannot send non-Latin-1 header values, and the ntfy server decodes MIME
    encoded words such as ``=?UTF-8?B?...?=`` in every header it reads.
    """
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def fit_utf8(text: str, limit: int) -> bytes:
    """Encode ``text`` as UTF-8 in at most ``limit`` bytes, cutting at a character boundary."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw
    marker = "\u2026".encode()
    return raw[: limit - len(marker)].decode("utf-8", errors="ignore").encode("utf-8") + marker


def telegram_text(note: Notification) -> str:
    """Plain text without ``parse_mode``, so nothing in the note is interpreted as markup."""
    parts = [note.title, note.message, note.url or ""]
    return truncate("\n\n".join(part for part in parts if part), TELEGRAM_MAX_TEXT)


def load_channels(settings: Mapping[str, Any]) -> dict[str, Channel]:
    """Parse ``[connectors.notify.channels.<name>]`` tables without resolving secrets or using the network."""
    tables = settings.get("channels", {})
    if not isinstance(tables, Mapping):
        raise ConfigError("[connectors.notify.channels] must be a table of channels")
    channels: dict[str, Channel] = {}
    for name, table in tables.items():
        label = f"[connectors.notify.channels.{name}]"
        if not isinstance(name, str) or not _CHANNEL_NAME.fullmatch(name):
            raise ConfigError(f"{label}: channel names use 1-64 letters, digits, '-' or '_'")
        if not isinstance(table, Mapping):
            raise ConfigError(f"{label} must be a table")
        channels[name] = _parse_channel(name, table, label)
    return channels


def _parse_channel(name: str, table: Mapping[str, Any], label: str) -> Channel:
    kind = table.get("type")
    if kind == "ntfy":
        return _parse_ntfy(name, table, label)
    if kind == "telegram":
        return _parse_telegram(name, table, label)
    if kind == "email":
        return _parse_email(name, table, label)
    raise ConfigError(f"{label}: type must be ntfy, telegram, or email")


def _parse_ntfy(name: str, table: Mapping[str, Any], label: str) -> NtfyChannel:
    allow_http = _flag(table, "allow_http", label)
    token = table.get("token")
    return NtfyChannel(
        name=name,
        topic=_reference(table.get("topic"), "topic", label),
        server=_server_url(table.get("server", NTFY_DEFAULT_SERVER), allow_http, label),
        token=None if token is None else _reference(token, "token", label),
        tags=_tags(table.get("tags", []), label),
        allow_http=allow_http,
    )


def _parse_telegram(name: str, table: Mapping[str, Any], label: str) -> TelegramChannel:
    chat_id = table.get("chat_id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
        raise ConfigError(f"{label}: chat_id must be a numeric chat id or an @channelusername")
    if not _TELEGRAM_CHAT.fullmatch(str(chat_id)):
        raise ConfigError(f"{label}: chat_id must be a numeric chat id or an @channelusername")
    return TelegramChannel(
        name=name, bot_token=_reference(table.get("bot_token"), "bot_token", label), chat_id=chat_id
    )


def _parse_email(name: str, table: Mapping[str, Any], label: str) -> EmailChannel:
    raw = table.get("to")
    addresses = [raw] if isinstance(raw, str) else raw
    if (
        not isinstance(addresses, list)
        or not 1 <= len(addresses) <= 10
        or not all(isinstance(item, str) and _ADDRESS.fullmatch(item) for item in addresses)
    ):
        raise ConfigError(f"{label}: to must list 1-10 of your own email addresses")
    account = table.get("account")
    if account is not None and not isinstance(account, str):
        raise ConfigError(f"{label}: account must name one of your mail accounts")
    return EmailChannel(name=name, to=tuple(addresses), account=account)


def _reference(value: Any, key: str, label: str) -> str:
    if not is_reference(value):
        raise ConfigError(
            f"{label}: {key} must be a secret reference such as env:NAME, file:/path, or cmd:program"
        )
    return str(value)


def _server_url(value: Any, allow_http: bool, label: str) -> str:
    message = f"{label}: server must be an https:// address without credentials, query, or fragment"
    if not isinstance(value, str):
        raise ConfigError(message)
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ConfigError(message) from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ConfigError(message)
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ConfigError(message)
    if parts.scheme == "http" and not allow_http:
        raise ConfigError(
            f"{label}: server uses http; use https, or set allow_http = true for a server on your own network"
        )
    return value.rstrip("/")


def _tags(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 5
        or not all(isinstance(tag, str) and _NTFY_TAG.fullmatch(tag) for tag in value)
    ):
        raise ConfigError(f'{label}: tags must list at most 5 short names such as "robot" or "warning"')
    return tuple(value)


def _flag(table: Mapping[str, Any], key: str, label: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: {key} must be true or false")
    return value


def _secret(ctx: AppContext, reference: str, label: str) -> str:
    try:
        return ctx.secret(reference)
    except SecretError as exc:
        raise ChannelError(f"the {label} reference does not resolve: {exc}") from exc


def _header_secret(ctx: AppContext, reference: str, label: str) -> str:
    value = _secret(ctx, reference, label)
    if not _HEADER_SAFE.fullmatch(value):
        raise ChannelError(f"the {label} contains spaces or control characters")
    return value


def _check_secret(ctx: AppContext, reference: str, label: str, shape: re.Pattern[str]) -> list[str]:
    """Problems with one secret reference for ``doctor``; never includes the value."""
    try:
        value = ctx.secret(reference)
    except SecretError as exc:
        return [f"{label}: {exc}"]
    if not shape.fullmatch(value):
        return [f"{label}: the secret resolves but does not have the expected form"]
    return []


def _post(
    ctx: AppContext,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes,
    allow_http: bool,
    host: str,
) -> HttpResponse:
    """POST without letting ``url`` reach an error message: it may carry a topic or a bot token.

    Any network failure is treated as possibly delivered: the request may have reached the service
    before the connection broke, so nothing is resent.
    """
    try:
        return ctx.http.request("POST", url, headers=headers, body=body, allow_http=allow_http)
    except HttpError as exc:
        raise ChannelUncertainError(
            f"could not reach {host} to confirm delivery; the note may have been delivered and was not resent"
        ) from exc


def _ambiguous(status: int) -> bool:
    """A reply after which the service may still have acted: a request timeout or a server error."""
    return status == 408 or status >= 500


def _ensure_telegram_ok(response: HttpResponse, bot: str) -> None:
    try:
        reply = response.json()
    except ValueError:
        reply = None
    if response.ok and isinstance(reply, dict) and reply.get("ok") is True:
        return
    description = reply.get("description") if isinstance(reply, dict) else None
    detail = ""
    if isinstance(description, str) and description:
        detail = ": " + truncate(description.replace(bot, "[redacted]"), 160)
    message = f"Telegram answered HTTP {response.status}{detail}"
    if response.ok or _ambiguous(response.status):
        # A 2xx reply that does not say ok:true, or a server error: the message may exist.
        raise ChannelUncertainError(f"{message}; the note may have been delivered and was not resent")
    raise ChannelError(message)
