"""The polling cursor: first run, new mail, UIDVALIDITY resets, the per-poll cap, and dedupe."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.connectors.mail.imap import ImapSession
from switchboard.connectors.mail.poll import Cursor, load_cursor, poll_mailbox, save_cursor
from switchboard.db import CORE_MIGRATIONS, Database
from switchboard.events import EventBus
from tests.fixtures.mail.fakes import FakeImap, FakeMailbox, simple_message

KEY = "personal:INBOX"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "mail-test.sqlite3")
    database.add_migrations("core", CORE_MIGRATIONS)
    database.migrate()
    return database


def poll(box: FakeMailbox, db: Database, events: EventBus, **options: int) -> int:
    return poll_mailbox(
        ImapSession(FakeImap([box])), db, events, account="personal", mailbox="INBOX", **options
    )


def test_first_poll_records_the_position_and_emits_nothing(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    for number in (1, 2, 3):
        box.add(simple_message(number))
    assert poll(box, db, events) == 0
    assert load_cursor(db, KEY) == Cursor(7, 3)
    assert events.pending() == []


def test_new_mail_becomes_one_event_per_message(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    for number in (1, 2, 3):
        box.add(simple_message(number))
    poll(box, db, events)
    box.add(simple_message(4))
    box.add(simple_message(5))
    assert poll(box, db, events) == 2
    pending = events.pending()
    assert [event.type for event in pending] == ["mail.received", "mail.received"]
    assert [event.payload["uid"] for event in pending] == [4, 5]
    payload = pending[0].payload
    assert payload["account"] == "personal"
    assert payload["mailbox"] == "INBOX"
    assert payload["message_id"] == "<m4@example.com>"
    assert payload["from"] == "Sender 4 <sender4@example.com>"
    assert payload["to"] == ["alex@example.com"]
    assert payload["subject"] == "Message 4"
    assert payload["text"] == "Body 4"
    assert payload["has_attachments"] is False
    assert payload["attachments"] == []
    assert load_cursor(db, KEY) == Cursor(7, 5)
    assert poll(box, db, events) == 0


def test_a_rewound_cursor_cannot_emit_duplicates(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    box.add(simple_message(1))
    poll(box, db, events)
    box.add(simple_message(2))
    assert poll(box, db, events) == 1
    save_cursor(db, KEY, Cursor(7, 1))
    assert poll(box, db, events) == 0
    assert len(events.pending()) == 1
    assert load_cursor(db, KEY) == Cursor(7, 2)


def test_uidvalidity_change_resets_the_cursor_without_emitting(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    for number in (1, 2, 3):
        box.add(simple_message(number))
    poll(box, db, events)
    box.uidvalidity = 8
    box.add(simple_message(4))
    assert poll(box, db, events) == 0
    assert load_cursor(db, KEY) == Cursor(8, 4)
    assert events.pending() == []


def test_each_poll_handles_at_most_the_limit_oldest_first(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    poll(box, db, events)
    assert load_cursor(db, KEY) == Cursor(7, 0)
    for number in range(1, 6):
        box.add(simple_message(number))
    assert poll(box, db, events, limit=2) == 2
    assert load_cursor(db, KEY) == Cursor(7, 2)
    assert poll(box, db, events, limit=2) == 2
    assert poll(box, db, events, limit=2) == 1
    assert poll(box, db, events, limit=2) == 0
    assert [event.payload["uid"] for event in events.pending()] == [1, 2, 3, 4, 5]


def test_uid_star_quirk_does_not_re_emit_the_newest_message(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=7)
    for number in (1, 2, 3):
        box.add(simple_message(number))
    save_cursor(db, KEY, Cursor(7, 3))
    box.uidnext = 10  # messages 4-9 were delivered and expunged; "UID 4:*" still matches UID 3
    assert poll(box, db, events) == 0
    assert events.pending() == []
    assert load_cursor(db, KEY) == Cursor(7, 3)


def test_missing_uidnext_falls_back_to_the_highest_uid(db: Database) -> None:
    events = EventBus(db)
    box = FakeMailbox("INBOX", uidvalidity=3, report_uidnext=False)
    box.add(simple_message(1))
    box.add(simple_message(2))
    assert poll(box, db, events) == 0
    assert load_cursor(db, KEY) == Cursor(3, 2)
    box.add(simple_message(3))
    assert poll(box, db, events) == 1


def test_unreadable_cursor_counts_as_a_first_run(db: Database) -> None:
    db.kv_set("mail", KEY, "not json")
    assert load_cursor(db, KEY) is None
    db.kv_set("mail", KEY, '{"uidvalidity": true, "last_uid": 1}')
    assert load_cursor(db, KEY) is None
