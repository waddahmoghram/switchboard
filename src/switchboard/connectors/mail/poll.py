"""The ``mail.poll`` job: turn new messages in watched mailboxes into ``mail.received`` events.

Each watched mailbox keeps a cursor in the key-value store (namespace ``mail``, key
``<account>:<mailbox>``) holding ``{"uidvalidity": …, "last_uid": …}``. UIDs only grow while
UIDVALIDITY stays the same (RFC 3501 section 2.3.1.1), so "UIDs above ``last_uid``" is exactly the new
mail. The first poll, and any poll that sees a new UIDVALIDITY, only records the current position and
emits nothing, so enabling the connector never floods rules with old mail. Each poll handles at most
:data:`MAX_PER_POLL` messages, oldest first; the rest wait for the next poll. Every event carries a
dedupe key, so a poll that is retried after a failure cannot emit the same message twice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from switchboard.connectors.mail.imap import ImapSession
from switchboard.connectors.mail.parse import ParsedMessage, parse_message
from switchboard.db import Database
from switchboard.events import EventBus

log = logging.getLogger(__name__)

NAMESPACE = "mail"
MAX_PER_POLL = 50
EVENT_TEXT_LIMIT = 20_000
EVENT_SOURCE = "mail"


@dataclass(frozen=True)
class Cursor:
    uidvalidity: int
    last_uid: int


def cursor_key(account: str, mailbox: str) -> str:
    return f"{account}:{mailbox}"


def load_cursor(db: Database, key: str) -> Cursor | None:
    """The stored cursor; a missing or unreadable value counts as a first run."""
    raw = db.kv_get(NAMESPACE, key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        uidvalidity, last_uid = data["uidvalidity"], data["last_uid"]
    except (ValueError, TypeError, KeyError):
        return None
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (uidvalidity, last_uid)):
        return None
    return Cursor(uidvalidity, last_uid)


def save_cursor(db: Database, key: str, cursor: Cursor) -> None:
    db.kv_set(NAMESPACE, key, json.dumps({"uidvalidity": cursor.uidvalidity, "last_uid": cursor.last_uid}))


def dedupe_key(account: str, mailbox: str, uidvalidity: int, uid: int) -> str:
    return f"mail:{account}:{mailbox}:{uidvalidity}:{uid}"


def received_payload(account: str, mailbox: str, uid: int, message: ParsedMessage) -> dict[str, Any]:
    """The ``mail.received`` payload described in docs/CONNECTOR_GUIDE.md (plus ``truncated``)."""
    headers = message.headers
    return {
        "account": account,
        "mailbox": mailbox,
        "uid": uid,
        "message_id": headers.message_id,
        "from": headers.sender,
        "to": headers.to,
        "cc": headers.cc,
        "subject": headers.subject,
        "date": headers.date,
        "text": message.text,
        "truncated": message.truncated,
        "has_attachments": bool(message.attachments),
        "attachments": [attachment.to_dict() for attachment in message.attachments],
    }


def poll_mailbox(
    session: ImapSession,
    db: Database,
    events: EventBus,
    *,
    account: str,
    mailbox: str,
    max_chars: int = EVENT_TEXT_LIMIT,
    limit: int = MAX_PER_POLL,
) -> int:
    """Emit events for new messages in one mailbox; returns how many new events were queued."""
    state = session.examine(mailbox)
    key = cursor_key(account, mailbox)
    previous = load_cursor(db, key)
    if previous is None or previous.uidvalidity != state.uidvalidity:
        start = state.uidnext - 1 if state.uidnext is not None else session.highest_uid()
        save_cursor(db, key, Cursor(state.uidvalidity, max(start, 0)))
        return 0
    if state.uidnext is not None and state.uidnext - 1 <= previous.last_uid:
        return 0
    emitted = 0
    last_uid = previous.last_uid
    try:
        for uid in session.uids_after(previous.last_uid)[:limit]:
            item = session.fetch_message(uid)
            if item is not None:
                try:
                    parsed = parse_message(item.data or b"", max_chars=min(max_chars, EVENT_TEXT_LIMIT))
                    payload = received_payload(account, mailbox, uid, parsed)
                except Exception as exc:  # one hostile message must not stall the mailbox forever
                    log.warning(
                        "mail %s/%s: skipped message %d (%s)", account, mailbox, uid, type(exc).__name__
                    )
                    last_uid = uid
                    continue
                key_for_event = dedupe_key(account, mailbox, state.uidvalidity, uid)
                if events.emit(EVENT_SOURCE, "mail.received", payload, dedupe_key=key_for_event) is not None:
                    emitted += 1
            last_uid = uid
    finally:
        if last_uid != previous.last_uid:
            save_cursor(db, key, Cursor(state.uidvalidity, last_uid))
    return emitted
