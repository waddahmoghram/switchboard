"""Building drafts and messages: headers, threading, recipients, and choosing the Drafts mailbox."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest

from switchboard.connectors.mail.compose import (
    Thread,
    check_addresses,
    choose_drafts_mailbox,
    complete_headers,
    compose,
    envelope_recipients,
    reply_thread,
    to_bytes,
)
from switchboard.connectors.mail.imap import ImapSession
from switchboard.connectors.mail.protocol import MailboxInfo
from switchboard.connectors.mail.service import store_draft
from switchboard.errors import ToolError
from tests.fixtures.mail.fakes import FakeImap, FakeMailbox, make_account

NOW = datetime(2026, 11, 11, 13, 45, tzinfo=UTC)


def lunch(**changes: Any) -> EmailMessage:
    arguments: dict[str, Any] = {
        "sender": "alex@example.com",
        "to": ["sam@example.org"],
        "cc": ["jordan@example.com"],
        "subject": "Lunch on Friday?",
        "body": "Does noon work?",
        "now": NOW,
        "message_id": "<fixed-1@example.com>",
    }
    arguments.update(changes)
    return compose(**arguments)


def test_compose_sets_the_standard_headers_and_never_writes_bcc() -> None:
    message = lunch()
    assert message["From"] == "alex@example.com"
    assert message["To"] == "sam@example.org"
    assert message["Cc"] == "jordan@example.com"
    assert message["Subject"] == "Lunch on Friday?"
    assert message["Date"] == "Wed, 11 Nov 2026 13:45:00 +0000"
    assert message["Message-ID"] == "<fixed-1@example.com>"
    assert message["Bcc"] is None
    assert message["In-Reply-To"] is None
    assert message.get_content() == "Does noon work?\n"


def test_generated_message_id_uses_the_sender_domain() -> None:
    message = lunch(message_id=None)
    assert str(message["Message-ID"]).endswith("@example.com>")


def test_reply_thread_follows_rfc_5322() -> None:
    thread = reply_thread("<b@example.com>", "<root@example.com> <a@example.com>")
    assert thread == Thread("<b@example.com>", "<root@example.com> <a@example.com> <b@example.com>")
    assert reply_thread("", "<a@example.com>") is None


def test_threading_headers_are_written() -> None:
    message = lunch(thread=Thread("<b@example.com>", "<a@example.com> <b@example.com>"))
    assert message["In-Reply-To"] == "<b@example.com>"
    assert message["References"] == "<a@example.com> <b@example.com>"


def test_header_injection_through_the_subject_is_refused() -> None:
    with pytest.raises(ToolError):
        lunch(subject="Hi\r\nBcc: someone@example.org")


def test_addresses_must_be_plain_and_are_deduplicated() -> None:
    with pytest.raises(ToolError):
        check_addresses(["Sam Example <sam@example.org>"])
    with pytest.raises(ToolError):
        check_addresses(["sam@example.org\n"])
    assert check_addresses(["a@example.com", "a@example.com", "b@example.org"]) == [
        "a@example.com",
        "b@example.org",
    ]


def test_messages_from_other_connectors_get_missing_headers_and_an_envelope() -> None:
    message = EmailMessage()
    message["To"] = "sam@example.org"
    message["Bcc"] = "records@example.com"
    message["Subject"] = "Digest"
    message.set_content("hello")
    complete_headers(message, sender="alex@example.com", now=NOW)
    assert message["From"] == "alex@example.com"
    assert message["Date"] == "Wed, 11 Nov 2026 13:45:00 +0000"
    assert str(message["Message-ID"]).endswith("@example.com>")
    assert envelope_recipients(message) == ["sam@example.org", "records@example.com"]


def test_a_message_without_recipients_cannot_be_sent() -> None:
    message = EmailMessage()
    message["Subject"] = "Nobody"
    with pytest.raises(ToolError, match="no recipients"):
        envelope_recipients(message)


def test_to_bytes_uses_crlf_line_endings() -> None:
    raw = to_bytes(lunch())
    assert raw.startswith(b"From: alex@example.com\r\n")
    assert b"\r\nSubject: Lunch on Friday?\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_drafts_mailbox_prefers_configuration_then_special_use_then_common_names() -> None:
    boxes = [MailboxInfo("INBOX", ()), MailboxInfo("Brouillons", ("\\Drafts",)), MailboxInfo("Drafts", ())]
    assert choose_drafts_mailbox(boxes, "Saved Drafts") == "Saved Drafts"
    assert choose_drafts_mailbox(boxes, None) == "Brouillons"
    assert choose_drafts_mailbox([MailboxInfo("INBOX", ()), MailboxInfo("drafts", ())], None) == "drafts"
    with pytest.raises(ToolError, match="drafts_mailbox"):
        choose_drafts_mailbox([MailboxInfo("INBOX", ())], None)


def test_unselectable_mailboxes_are_never_chosen() -> None:
    boxes = [MailboxInfo("[Gmail]", ("\\Noselect", "\\Drafts")), MailboxInfo("[Gmail]/Drafts", ())]
    assert choose_drafts_mailbox(boxes, None) == "[Gmail]/Drafts"


def test_store_draft_appends_with_the_draft_flag_to_the_special_use_mailbox() -> None:
    fake = FakeImap(
        [FakeMailbox("INBOX"), FakeMailbox("Brouillons", attributes=("\\HasNoChildren", "\\Drafts"))]
    )
    mailbox, uid = store_draft(ImapSession(fake), make_account(), lunch(), NOW)
    assert (mailbox, uid) == ("Brouillons", 1)
    assert ("LIST", '""', "* RETURN (SPECIAL-USE)") in fake.calls
    appended = fake.appended[0]
    assert appended["mailbox"] == "Brouillons"
    assert appended["flags"] == "\\Draft"
    assert appended["date_time"] == '"11-Nov-2026 13:45:00 +0000"'
    assert appended["message"].startswith(b"From: alex@example.com\r\n")


def test_store_draft_uses_the_configured_mailbox_without_listing() -> None:
    fake = FakeImap([FakeMailbox("Entw&APw-rfe")], capabilities="IMAP4rev1")
    mailbox, uid = store_draft(ImapSession(fake), make_account(drafts_mailbox="Entwürfe"), lunch(), NOW)
    assert (mailbox, uid) == ("Entwürfe", 1)
    assert not any(call[0] == "LIST" for call in fake.calls)
    assert ("APPEND", '"Entw&APw-rfe"', "\\Draft") in fake.calls
