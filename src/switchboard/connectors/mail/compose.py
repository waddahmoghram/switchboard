"""Build outgoing messages, thread replies, and choose the Drafts mailbox."""

from __future__ import annotations

import email.policy
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, make_msgid

from switchboard.connectors.mail.accounts import ADDRESS
from switchboard.connectors.mail.protocol import MailboxInfo, reject_controls
from switchboard.errors import ToolError

MAX_RECIPIENTS = 100
MAX_REFERENCES = 30
COMMON_DRAFTS_NAMES = ("Drafts", "INBOX.Drafts", "INBOX/Drafts", "Draft", "[Gmail]/Drafts")


@dataclass(frozen=True)
class Thread:
    """Threading headers for a reply (RFC 5322 section 3.6.4)."""

    in_reply_to: str
    references: str


def reply_thread(message_id: str, references: str) -> Thread | None:
    """In-Reply-To is the parent's Message-ID; References is the parent's References plus that ID."""
    parent = message_id.strip()
    if not parent:
        return None
    chain = [*references.split(), parent][-MAX_REFERENCES:]
    return Thread(in_reply_to=parent, references=" ".join(chain))


def check_addresses(addresses: Sequence[str]) -> list[str]:
    """Validate plain addresses (no display names); duplicates are removed, order is kept."""
    for address in addresses:
        if not ADDRESS.match(address):
            raise ToolError(f"not a plain email address: {address[:80]!r}")
    unique = list(dict.fromkeys(addresses))
    if len(unique) > MAX_RECIPIENTS:
        raise ToolError(f"at most {MAX_RECIPIENTS} recipients per message")
    return unique


def compose(
    *,
    sender: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    subject: str,
    body: str,
    now: datetime,
    thread: Thread | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """A plain-text message with Date and Message-ID set. Bcc is never written into the headers."""
    reject_controls(subject, "the subject")
    message = EmailMessage(policy=email.policy.default)
    message["From"] = sender
    message["To"] = ", ".join(check_addresses(to))
    if cc:
        message["Cc"] = ", ".join(check_addresses(cc))
    message["Subject"] = subject
    message["Date"] = format_datetime(now)
    message["Message-ID"] = message_id or new_message_id(sender)
    if thread is not None:
        message["In-Reply-To"] = thread.in_reply_to
        message["References"] = thread.references
    message.set_content(body)
    return message


def new_message_id(sender: str) -> str:
    """A fresh Message-ID on the sender's domain, so the local host name is not disclosed."""
    return make_msgid(domain=sender.rpartition("@")[2] or "switchboard.invalid")


def complete_headers(message: EmailMessage, *, sender: str, now: datetime) -> EmailMessage:
    """Fill in From, Date, and Message-ID when a message from another connector lacks them."""
    if message.get("From") is None:
        message["From"] = sender
    if message.get("Date") is None:
        message["Date"] = format_datetime(now)
    if message.get("Message-ID") is None:
        message["Message-ID"] = new_message_id(sender)
    return message


def envelope_recipients(message: EmailMessage) -> list[str]:
    """Every To, Cc, and Bcc address of a message, validated, for the SMTP envelope."""
    values = [str(value) for name in ("To", "Cc", "Bcc") for value in message.get_all(name, [])]
    addresses = [address for _display, address in getaddresses(values) if address]
    if not addresses:
        raise ToolError("the message has no recipients")
    return check_addresses(addresses)


def to_bytes(message: EmailMessage) -> bytes:
    """Serialize with CRLF line endings, as IMAP APPEND expects."""
    return message.as_bytes(policy=email.policy.SMTP)


def choose_drafts_mailbox(mailboxes: Sequence[MailboxInfo], configured: str | None) -> str:
    """Pick where drafts go: the configured name, else the RFC 6154 ``\\Drafts`` mailbox, else a
    commonly used name. Raises :class:`ToolError` when nothing fits."""
    if configured:
        return configured
    for mailbox in mailboxes:
        if mailbox.selectable and mailbox.has_flag("\\Drafts"):
            return mailbox.name
    by_lower = {mailbox.name.lower(): mailbox.name for mailbox in mailboxes if mailbox.selectable}
    for candidate in COMMON_DRAFTS_NAMES:
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    raise ToolError("could not find a Drafts mailbox; set drafts_mailbox for this account")
