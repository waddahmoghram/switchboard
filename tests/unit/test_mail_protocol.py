"""IMAP syntax helpers: quoting, modified UTF-7, SEARCH criteria, and imaplib response shapes."""

from __future__ import annotations

from datetime import date

import pytest

from switchboard.connectors.mail.protocol import (
    build_search,
    decode_mailbox,
    encode_mailbox,
    imap_date,
    mailbox_argument,
    parse_appenduid,
    parse_fetch,
    parse_list,
    parse_search,
    quote,
)
from switchboard.errors import ToolError


def test_quote_escapes_backslash_and_double_quote() -> None:
    assert quote('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


@pytest.mark.parametrize("value", ["a\r\nb", "a\nb", "a\rb", "a\x00b"])
def test_quote_refuses_line_breaks_and_nul(value: str) -> None:
    with pytest.raises(ToolError):
        quote(value)


def test_crlf_in_a_search_term_cannot_start_a_new_command() -> None:
    with pytest.raises(ToolError, match="line breaks"):
        build_search(subject='x"\r\nA1 DELETE INBOX')


def test_a_quote_in_a_search_term_stays_inside_one_string() -> None:
    query = build_search(sender='a" OR ALL "')
    assert query.arguments() == ("FROM", '"a\\" OR ALL \\""')


def test_search_criteria_order_and_dates() -> None:
    query = build_search(
        sender="alex@example.com",
        subject="Planning",
        since=date(2026, 11, 2),
        before=date(2026, 11, 10),
        unseen=True,
    )
    assert query.arguments() == (
        "FROM",
        '"alex@example.com"',
        "SUBJECT",
        '"Planning"',
        "SINCE",
        "2-Nov-2026",
        "BEFORE",
        "10-Nov-2026",
        "UNSEEN",
    )
    assert query.charset is None
    assert query.literal is None


def test_no_criteria_means_all() -> None:
    assert build_search().arguments() == ("ALL",)


def test_one_non_ascii_term_is_sent_last_as_a_utf8_literal() -> None:
    query = build_search(subject="Café", sender="alex@example.com")
    assert query.arguments() == ("CHARSET", "UTF-8", "FROM", '"alex@example.com"', "SUBJECT")
    assert query.literal == "Café".encode()


def test_a_second_non_ascii_term_is_refused() -> None:
    with pytest.raises(ToolError, match="only one"):
        build_search(sender="José", subject="Café")


def test_imap_date_is_locale_independent() -> None:
    assert imap_date(date(2026, 1, 5)) == "5-Jan-2026"


def test_modified_utf7_matches_the_rfc_3501_examples() -> None:
    assert encode_mailbox("~peter/mail/台北/日本語") == "~peter/mail/&U,BTFw-/&ZeVnLIqe-"
    assert decode_mailbox("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"
    assert encode_mailbox("台北日本語") == "&U,BTF2XlZyyKng-"  # one shift, not "&U,BTFw-&ZeVnLIqe-"
    assert decode_mailbox("&Jjo-!") == "☺!"


def test_modified_utf7_ampersand_and_umlaut() -> None:
    assert encode_mailbox("R&D") == "R&-D"
    assert encode_mailbox("Entwürfe") == "Entw&APw-rfe"
    assert decode_mailbox("Entw&APw-rfe") == "Entwürfe"


@pytest.mark.parametrize(
    "name", ["INBOX", "R&D", "Entwürfe", "Projects/2026 & beyond", "📁 Archive", "a&b&c"]
)
def test_modified_utf7_round_trips(name: str) -> None:
    assert decode_mailbox(encode_mailbox(name)) == name


def test_malformed_modified_utf7_is_kept_verbatim() -> None:
    assert decode_mailbox("Bad&*-name") == "Bad&*-name"
    assert decode_mailbox("Unterminated&AOk") == "Unterminated&AOk"


def test_mailbox_argument_encodes_then_quotes() -> None:
    assert mailbox_argument("Entwürfe") == '"Entw&APw-rfe"'
    assert mailbox_argument('Saved "Mail"') == '"Saved \\"Mail\\""'


def test_mailbox_argument_refuses_line_breaks() -> None:
    with pytest.raises(ToolError):
        mailbox_argument("INBOX\r\nA2 LOGOUT")


@pytest.mark.parametrize(
    ("data", "expected"),
    [([b"4 9 12"], [4, 9, 12]), ([b""], []), ([None], []), ([b"12 4", b"9"], [4, 9, 12])],
)
def test_parse_search(data: list[bytes | None], expected: list[int]) -> None:
    assert parse_search(data) == expected


def test_parse_list_reads_flags_quoting_literals_and_utf7() -> None:
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
        b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
        b'(\\HasNoChildren) "/" Archive',
        b'(\\HasNoChildren) NIL "Entw&APw-rfe"',
        (b'(\\HasNoChildren) "/" {10}', b'Odd "Name"'),
        b"",
    ]
    mailboxes = parse_list(data)
    assert [mailbox.name for mailbox in mailboxes] == [
        "INBOX",
        "[Gmail]/Drafts",
        "[Gmail]",
        "Archive",
        "Entwürfe",
        'Odd "Name"',
    ]
    assert mailboxes[1].has_flag("\\drafts")
    assert mailboxes[1].selectable
    assert not mailboxes[2].selectable


def test_parse_fetch_merges_trailers_and_skips_unsolicited_updates() -> None:
    data = [
        (
            b"1 (UID 101 FLAGS (\\Seen) RFC822.SIZE 2048 BODY[HEADER.FIELDS (SUBJECT)] {18}",
            b"Subject: First\r\n\r\n",
        ),
        b")",
        (b"2 (UID 102 RFC822.SIZE 4096 BODY[HEADER.FIELDS (SUBJECT)] {19}", b"Subject: Second\r\n\r\n"),
        b" FLAGS (\\Answered \\Flagged))",
        b"3 (FLAGS (\\Seen))",
    ]
    found = parse_fetch(data)
    assert sorted(found) == [101, 102]
    assert found[101].flags == ("\\Seen",)
    assert found[101].size == 2048
    assert found[101].data == b"Subject: First\r\n\r\n"
    assert found[102].flags == ("\\Answered", "\\Flagged")
    assert found[102].data == b"Subject: Second\r\n\r\n"


def test_parse_fetch_without_literal() -> None:
    found = parse_fetch([b"7 (UID 300 RFC822.SIZE 12 FLAGS ())"])
    assert found[300].size == 12
    assert found[300].flags == ()
    assert found[300].data is None


def test_parse_appenduid() -> None:
    assert parse_appenduid([b"[APPENDUID 38505 3955] APPEND completed"]) == (38505, 3955)
    assert parse_appenduid([b"APPEND completed"]) is None
