"""APPEND outcomes: a refusal is definite, a connection lost mid-command leaves the draft's fate unknown."""

from __future__ import annotations

import imaplib
from datetime import UTC, datetime

import pytest

from switchboard.connectors.mail.imap import ImapSession
from switchboard.errors import ToolError, UncertainOutcomeError
from tests.fixtures.mail.fakes import FakeImap, FakeMailbox

NOW = datetime(2026, 11, 11, 13, 45, tzinfo=UTC)
DRAFT = b"From: alex@example.com\r\nSubject: Draft\r\n\r\nHello\r\n"


def _drafts() -> FakeImap:
    return FakeImap([FakeMailbox("INBOX"), FakeMailbox("Drafts", uidvalidity=7)])


def test_a_stored_draft_reports_its_uid() -> None:
    conn = _drafts()
    assert ImapSession(conn).append("Drafts", DRAFT, flags="(\\Draft)", when=NOW) == 1
    assert conn.appended[0]["mailbox"] == "Drafts"


def test_a_refused_append_is_a_definite_failure() -> None:
    conn = _drafts()
    with pytest.raises(ToolError, match="APPEND failed") as caught:
        ImapSession(conn).append("Missing", DRAFT, flags="(\\Draft)", when=NOW)
    assert not isinstance(caught.value, UncertainOutcomeError)


@pytest.mark.parametrize(
    "failure",
    [imaplib.IMAP4.abort("socket error: EOF"), TimeoutError("timed out"), ConnectionResetError("reset")],
)
def test_a_connection_lost_during_append_is_uncertain(failure: BaseException) -> None:
    class Dropping(FakeImap):
        def append(self, mailbox: str, flags: str, date_time: str, message: bytes) -> tuple[str, list[bytes]]:
            self.calls.append(("APPEND", mailbox, flags))
            raise failure

    conn = Dropping([FakeMailbox("Drafts")])
    with pytest.raises(UncertainOutcomeError, match="may have stored the draft") as caught:
        ImapSession(conn).append("Drafts", DRAFT, flags="(\\Draft)", when=NOW)
    assert caught.value.__cause__ is failure
    assert conn.calls == [("APPEND", '"Drafts"', "(\\Draft)")]


def test_a_protocol_error_reply_is_not_mistaken_for_a_lost_connection() -> None:
    class Bad(FakeImap):
        def append(self, mailbox: str, flags: str, date_time: str, message: bytes) -> tuple[str, list[bytes]]:
            raise imaplib.IMAP4.error("APPEND command error: BAD [b'invalid date']")

    with pytest.raises(imaplib.IMAP4.error) as caught:
        ImapSession(Bad([FakeMailbox("Drafts")])).append("Drafts", DRAFT, flags="(\\Draft)", when=NOW)
    assert not isinstance(caught.value, imaplib.IMAP4.abort)
