"""Message parsing: decoded headers, plain-text preference, HTML fallback, attachment metadata."""

from __future__ import annotations

import pytest

from switchboard.connectors.mail.parse import html_to_text, parse_headers, parse_message
from tests.fixtures.mail.fakes import load


def test_plain_message() -> None:
    parsed = parse_message(load("plain.eml"), max_chars=20_000)
    headers = parsed.headers
    assert headers.sender == "Alex Example <alex@example.com>"
    assert headers.to == ["Sam Example <sam@example.org>", "team@example.org"]
    assert headers.cc == ["Jordan Example <jordan@example.com>"]
    assert headers.subject == "Quarterly planning notes"
    assert headers.message_id == "<plain-1@example.com>"
    assert headers.date == "2026-11-10T14:30:00+00:00"
    assert parsed.text.startswith("Hi Sam,\n\nHere are the notes from today's planning call.")
    assert parsed.text.endswith("Thanks,\nAlex")
    assert parsed.truncated is False
    assert parsed.attachments == []


def test_text_is_capped_and_flagged() -> None:
    parsed = parse_message(load("plain.eml"), max_chars=10)
    assert parsed.text == "Hi Sam,\n\nH"
    assert parsed.truncated is True


def test_html_only_message_becomes_readable_text() -> None:
    parsed = parse_message(load("html_only.eml"), max_chars=20_000)
    text = parsed.text
    assert parsed.headers.sender == "Example Store <orders@shop.example.com>"
    assert text.startswith("Order shipped\n")
    assert "Your order #1234 is on its way." in text
    assert "Track it here: tracking" in text
    assert "Item Qty" in text
    assert "Notebook 2" in text
    for hidden in ("alert", "color", "Shipping", "<", "&nbsp;"):
        assert hidden not in text
    assert "\n\n\n" not in text


def test_html_to_text_drops_scripts_and_keeps_block_breaks() -> None:
    markup = (
        "<p>Hello&nbsp;<b>World</b></p><script>bad()</script><style>p{}</style>"
        "<div>Line one<br>Line two</div>"
    )
    assert html_to_text(markup) == "Hello World\n\nLine one\nLine two"


def test_html_head_without_closing_tag_still_shows_the_body() -> None:
    assert html_to_text("<html><head><title>Ignored</title><body><p>Visible</p></body></html>") == "Visible"


def test_multipart_prefers_plain_text_and_reports_attachments_only() -> None:
    parsed = parse_message(load("multipart_attachment.eml"), max_chars=20_000)
    assert parsed.text == "Please find the invoice attached."
    assert [attachment.to_dict() for attachment in parsed.attachments] == [
        {"filename": "invoice-1001.pdf", "content_type": "application/pdf", "size": 45}
    ]


def test_rfc2047_headers_and_rfc2231_filenames_are_decoded() -> None:
    parsed = parse_message(load("encoded_headers.eml"), max_chars=20_000)
    assert parsed.headers.sender == "José Example <jose@example.com>"
    assert parsed.headers.to == ["Élodie Example <elodie@example.org>"]
    assert parsed.headers.subject == "Café meeting ☕ tomorrow"
    assert parsed.text == "Bientôt au café."
    assert [(item.filename, item.content_type, item.size) for item in parsed.attachments] == [
        ("résumé.txt", "text/plain", 16)
    ]


def test_parse_headers_reads_a_header_block() -> None:
    headers = parse_headers(
        b"Subject: Hello\r\nFrom: Alex Example <alex@example.com>\r\nMessage-ID: <h-1@example.com>\r\n\r\n"
    )
    assert headers.subject == "Hello"
    assert headers.sender == "Alex Example <alex@example.com>"
    assert headers.message_id == "<h-1@example.com>"
    assert headers.to == []
    assert headers.date is None


@pytest.mark.parametrize(
    "date",
    [
        "Fri, 31 Dec 9999 23:59:59 -2359",  # valid, but past the last UTC instant Python can hold
        "not a date",
    ],
)
def test_dates_that_cannot_be_shown_in_utc_become_none(date: str) -> None:
    headers = parse_headers(f"Subject: x\r\nDate: {date}\r\n\r\n".encode())
    assert headers.subject == "x"
    assert headers.date is None


def test_control_characters_are_removed_from_headers_and_text() -> None:
    raw = b"Subject: Alert \x1b[31mred\r\nFrom: alex@example.com\r\n\r\nBody \x07with bell\r\n"
    parsed = parse_message(raw, max_chars=100)
    assert "\x1b" not in parsed.headers.subject
    assert parsed.text == "Body with bell"


def test_garbage_input_does_not_raise() -> None:
    parsed = parse_message(b"\x00\xff not a message", max_chars=100)
    assert parsed.headers.subject == ""
    assert parsed.attachments == []
