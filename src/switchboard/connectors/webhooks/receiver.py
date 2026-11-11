"""Turn verified deliveries into ``webhook.received`` events; refuse and audit everything else."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from switchboard.connectors.webhooks.hooks import Hook, WebhookSettings
from switchboard.connectors.webhooks.verify import (
    Reply,
    Request,
    parse_json,
    safe_headers,
    verify,
    without_keys,
)
from switchboard.context import AppContext
from switchboard.db import transaction
from switchboard.errors import SecretError
from switchboard.util import iso, truncate

log = logging.getLogger(__name__)

HOOK_PREFIX = "/hooks/"
EVENT_TYPE = "webhook.received"
MAX_TEXT = 20_000
MAX_CONTENT_TYPE = 200


class WebhookReceiver:
    """Verify one delivery, then emit an event, answer a handshake, or refuse it with an audit record."""

    def __init__(self, ctx: AppContext, settings: WebhookSettings, *, source: str) -> None:
        self.ctx = ctx
        self.settings = settings
        self.source = source
        self._secrets: dict[str, str] = {}

    def hook_for_path(self, path: str) -> Hook | None:
        """The hook served at ``/hooks/<name>``, or None for any other path."""
        if not path.startswith(HOOK_PREFIX):
            return None
        return self.settings.hooks.get(path[len(HOOK_PREFIX) :])

    def load_secrets(self) -> list[str]:
        """Resolve every hook's secret now (and cache it); return problems, never values."""
        problems: list[str] = []
        for hook in self.settings.hooks.values():
            try:
                self._secret(hook)
            except SecretError as exc:
                problems.append(f"hook {hook.name}: {exc}")
        return problems

    def handle(self, hook: Hook, request: Request, *, now: float) -> Reply:
        """Process one delivery that already passed the size limit. ``now`` is Unix seconds."""
        try:
            secret = self._secret(hook)
        except SecretError:
            log.error("webhook %s: its secret reference does not resolve", hook.name)
            return Reply(503, "receiver is not configured\n")
        check = verify(hook, secret, request, now=now)
        if not check.ok:
            return self._refuse(hook, request, check.reason)
        if check.reply is not None:
            log.info("webhook %s: answered a handshake", hook.name)
            return check.reply
        if check.replay_key is not None and not self._first_sighting(
            hook.name, check.replay_key, check.replay_until, now
        ):
            return self._refuse(hook, request, "replayed delivery")
        payload = event_payload(hook, request, now=now, drop_keys=check.drop_keys)
        dedupe = None if check.dedupe is None else f"{self.source}:{hook.name}:{check.dedupe}"
        event_id = self.ctx.events.emit(self.source, EVENT_TYPE, payload, dedupe_key=dedupe)
        if event_id is None:
            log.info("webhook %s: duplicate delivery ignored", hook.name)
            return Reply(202, "duplicate\n")
        log.info("webhook %s: accepted %d bytes as event %d", hook.name, len(request.body), event_id)
        return Reply(202, "accepted\n")

    def _secret(self, hook: Hook) -> str:
        cached = self._secrets.get(hook.name)
        if cached is None:
            cached = self.ctx.secret(hook.secret)
            self._secrets[hook.name] = cached
        return cached

    def _refuse(self, hook: Hook, request: Request, reason: str) -> Reply:
        # The audit record names the hook and the reason, never the body or the headers.
        self.ctx.audit.append("webhook.rejected", hook=hook.name, reason=reason, size=len(request.body))
        log.warning("webhook %s: refused (%s)", hook.name, reason)
        return Reply(401, "verification failed\n")

    def _first_sighting(self, hook: str, key: str, until: int, now: float) -> bool:
        """Record ``key`` until ``until``; False if it is already on record (a replay)."""
        with self.ctx.db.session() as conn, transaction(conn):
            conn.execute("DELETE FROM webhooks_seen WHERE expires_at <= ?", (int(now),))
            cursor = conn.execute(
                "INSERT OR IGNORE INTO webhooks_seen (hook, replay_key, expires_at) VALUES (?, ?, ?)",
                (hook, key, until),
            )
            return cursor.rowcount == 1


def event_payload(hook: Hook, request: Request, *, now: float, drop_keys: frozenset[str]) -> dict[str, Any]:
    """The ``webhook.received`` payload: JSON bodies as ``json``, anything else as capped ``text``."""
    payload: dict[str, Any] = {
        "hook": hook.name,
        "received_at": iso(datetime.fromtimestamp(now, UTC)),
        "content_type": truncate(request.headers.get("content-type", ""), MAX_CONTENT_TYPE),
        "headers": safe_headers(request.headers, drop=[hook.header] if hook.header else []),
        "size": len(request.body),
    }
    is_json, document = parse_json(request.body)
    if is_json:
        payload["json"] = without_keys(document, drop_keys) if drop_keys else document
    else:
        payload["text"] = truncate(request.body.decode("utf-8", errors="replace"), MAX_TEXT)
    return payload
