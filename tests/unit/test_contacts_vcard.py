"""Unit tests for the vCard reader (RFC 6350 vCard 4.0 and RFC 2426 vCard 3.0)."""

from __future__ import annotations

from pathlib import Path

from switchboard.connectors.contacts import vcard

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"


def _cards() -> tuple[list[vcard.Card], list[str]]:
    return vcard.parse_cards((FIXTURES / "contacts.vcf").read_text(encoding="utf-8"))


def test_vcard_3_with_groups_types_and_escapes() -> None:
    cards, _ = _cards()
    alex = cards[0]
    assert alex.uid == "card-alex@example.invalid"
    assert alex.fn == "Dr. Alex J. Rivera"
    assert alex.org == "Example Labs, Research"
    assert alex.emails == (
        vcard.Entry("alex.rivera@example.invalid", ("work", "pref")),
        vcard.Entry("alex@home.example.invalid", ()),
    )
    assert alex.phones == (
        vcard.Entry("+1 555-0100", ("cell",)),
        vcard.Entry("+1 (555) 0101", ("work", "voice")),
    )
    assert alex.bday == "1985-04-12"
    assert alex.note == "Met at the synthetic conference, booth 4.\nPrefers email."


def test_vcard_4_quoted_types_pref_tel_uri_and_folding() -> None:
    cards, _ = _cards()
    sam = cards[1]
    assert sam.uid == "urn:uuid:4f8b2c1e-0000-4000-8000-000000000002"
    assert sam.fn == "Sam Example"
    assert sam.emails == (vcard.Entry("sam@example.invalid", ("home", "pref")),)
    assert sam.phones == (vcard.Entry("+44-20-7946-0000", ("voice", "cell")),)
    assert sam.org == "Example Co"
    assert sam.note == "A long note that is folded across two physical lines in the file to check unfolding."


def test_missing_fn_falls_back_to_n_and_missing_uid_to_a_content_hash() -> None:
    cards, warnings = _cards()
    assert len(cards) == 3
    kim = cards[2]
    assert kim.fn == "Kim Nobody"
    assert kim.emails == (vcard.Entry("kim@example.invalid", ("home",)),)
    assert kim.uid.startswith("sha256:")
    assert warnings == ["skipped a card without a name, email, or phone"]


def test_notes_are_capped() -> None:
    text = "BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Long Note\r\nNOTE:" + "n" * 2000 + "\r\nEND:VCARD\r\n"
    [card], _ = vcard.parse_cards(text)
    assert len(card.note) == vcard.NOTE_LIMIT
    assert card.note.endswith("…")


def test_parse_line_keeps_colons_inside_quoted_parameters() -> None:
    line = vcard.parse_line(
        'item2.ADR;LABEL="12 Example St: Unit 3";TYPE=home:;;12 Example St;Springfield;;;'
    )
    assert line is not None
    assert line.name == "ADR"
    assert line.params["LABEL"] == ["12 Example St: Unit 3"]
    assert line.value == ";;12 Example St;Springfield;;;"
    assert vcard.parse_line("no separator here") is None
