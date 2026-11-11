"""OFX 1.x SGML, OFX 2.x XML, and QFX parsing."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from switchboard.connectors.finance.models import Balance, FormatError
from switchboard.connectors.finance.money import parse_ofx_amount
from switchboard.connectors.finance.ofx import (
    build_tree,
    decode_ofx,
    is_ofx,
    parse_ofx,
    parse_ofx_date,
    strip_markup,
)
from switchboard.connectors.finance.privacy import last4_of

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"

SGML_1252 = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>000000000
<ACCTID>99887766
<ACCTTYPE>SAVINGS
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>POS
<DTPOSTED>20261105233000[-8:PST]
<TRNAMT>-4,75
<FITID>A1
<MEMO>
<NAME>CAF\u00c9 EXAMPLE \u2019S
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""

XML_TWO_STATEMENTS = b"""<?xml version="1.0" encoding="USASCII"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD</CURDEF>
<BANKACCTFROM>
<BANKID>000000000</BANKID><ACCTID>11112222</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE>
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261101</DTPOSTED><TRNAMT>-10.00</TRNAMT>
<FITID>B1</FITID><NAME>EXAMPLE PARKING</NAME>
</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>33334444</ACCTID></CCACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261102</DTPOSTED><TRNAMT>-20.00</TRNAMT>
<FITID>C1</FITID><NAME>EXAMPLE BOOKS</NAME><MEMO/>
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""


def test_sgml_statement_with_unclosed_elements() -> None:
    [statement] = parse_ofx((FIXTURES / "example_cu_checking.ofx").read_bytes())

    assert statement.kind == "bank"
    assert statement.account_type == "CHECKING"
    assert statement.currency == "USD"
    assert last4_of(statement.account_number) == "6789"
    assert [txn.amount_cents for txn in statement.transactions] == [250000, -8412, -120000, -1549]
    assert [txn.posted for txn in statement.transactions] == [
        date(2026, 10, 15),
        date(2026, 10, 20),
        date(2026, 11, 2),
        date(2026, 11, 4),
    ]
    grocery, check = statement.transactions[1], statement.transactions[2]
    assert (grocery.description, grocery.memo, grocery.kind) == (
        "POS DEBIT 1020 EXAMPLE MARKET",
        "GROCERY PURCHASE",
        "POS",
    )
    assert grocery.external_id == "ECU-20261020-001"
    assert (check.check_number, check.kind) == ("1044", "CHECK")
    assert statement.ledger_balance == Balance(320039, date(2026, 11, 5))
    assert statement.available_balance == Balance(310039, date(2026, 11, 5))


def test_charset_1252_comma_decimal_and_empty_sgml_element() -> None:
    raw = SGML_1252.encode("cp1252")

    [statement] = parse_ofx(raw)

    [txn] = statement.transactions
    assert statement.account_type == "SAVINGS"
    assert txn.description == "CAF\u00c9 EXAMPLE \u2019S"
    assert txn.memo == ""
    assert txn.amount_cents == -475
    # 23:30 Pacific is already the next day in UTC; the statement's calendar date is kept.
    assert txn.posted == date(2026, 11, 5)


def test_credit_card_statement_in_ofx_2_xml_qfx() -> None:
    [statement] = parse_ofx((FIXTURES / "example_card.qfx").read_bytes())

    assert statement.kind == "credit_card"
    assert statement.account_type == "CREDITCARD"
    assert last4_of(statement.account_number) == "4321"
    purchase, payment = statement.transactions
    assert (purchase.description, purchase.amount_cents) == ("SMITH & SONS HARDWARE", -4250)
    assert (payment.kind, payment.amount_cents) == ("CREDIT", 30000)
    assert statement.ledger_balance == Balance(-51234, date(2026, 11, 6))
    assert statement.available_balance is None


def test_xml_file_with_bank_and_card_statements_and_unknown_encoding_label() -> None:
    bank, card = parse_ofx(XML_TWO_STATEMENTS)

    assert (bank.kind, bank.transactions[0].amount_cents) == ("bank", -1000)
    assert (card.kind, last4_of(card.account_number)) == ("credit_card", "4444")
    assert card.transactions[0].description == "EXAMPLE BOOKS"


def test_account_number_is_not_in_the_statement_repr() -> None:
    [statement] = parse_ofx((FIXTURES / "example_cu_checking.ofx").read_bytes())

    assert "123456789" not in repr(statement)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20261105", date(2026, 11, 5)),
        ("20261105120000", date(2026, 11, 5)),
        ("20261105120000.000[-8:PST]", date(2026, 11, 5)),
        ("20261105233000.123[+5.30:IST]", date(2026, 11, 5)),
        ("20261231235959[0:GMT]", date(2026, 12, 31)),
    ],
)
def test_ofx_dates_keep_the_calendar_date(value: str, expected: date) -> None:
    assert parse_ofx_date(value) == expected


@pytest.mark.parametrize("value", ["2026-11-05", "202611", "20261340", "20261105[-8:PST"])
def test_malformed_ofx_dates_are_rejected(value: str) -> None:
    with pytest.raises(FormatError):
        parse_ofx_date(value)


@pytest.mark.parametrize(
    ("text", "cents"), [("-80", -8000), ("+5", 500), ("12,50", 1250), (" -0.10 ", -10), ("1.005", 101)]
)
def test_ofx_amounts(text: str, cents: int) -> None:
    assert parse_ofx_amount(text) == cents


@pytest.mark.parametrize("text", ["1,234.56", "NaN", "1e3", ""])
def test_ofx_amounts_reject_grouping_and_special_values(text: str) -> None:
    with pytest.raises(ValueError, match="not a plain decimal"):
        parse_ofx_amount(text)


def test_files_without_statements_or_amounts_are_errors() -> None:
    with pytest.raises(FormatError, match="no <OFX> element"):
        parse_ofx(b"Date,Description,Amount\n")
    with pytest.raises(FormatError, match="no bank or credit card statement"):
        parse_ofx(b"OFXHEADER:100\n\n<OFX>\n<SIGNONMSGSRSV1>\n</SIGNONMSGSRSV1>\n</OFX>\n")
    missing_amount = XML_TWO_STATEMENTS.replace(b"<TRNAMT>-10.00</TRNAMT>", b"")
    with pytest.raises(FormatError, match="missing DTPOSTED or TRNAMT"):
        parse_ofx(missing_amount)


def test_decoding_follows_the_declared_encoding() -> None:
    utf8 = '<?xml version="1.0" encoding="UTF-8"?><OFX><NAME>CAF\u00c9</NAME></OFX>'.encode()
    bom = b"\xef\xbb\xbf" + utf8

    assert "CAF\u00c9" in decode_ofx(utf8)
    assert decode_ofx(bom).startswith("<?xml")
    assert is_ofx(b"OFXHEADER:100\n") and not is_ofx(b"Date,Amount\n")


def test_entities_are_decoded_but_nothing_is_expanded() -> None:
    root = build_tree("<OFX><NAME>A &amp; B &#38; C &#x26; D &bogus; &#xD800;</NAME></OFX>")

    assert root.find_text("NAME") == "A & B & C & D &bogus; \ufffd"


def test_deep_nesting_is_refused() -> None:
    with pytest.raises(FormatError, match="nested too deeply"):
        build_tree("<A>" * 70 + "</A>" * 70)


@pytest.mark.parametrize("label", ["base64", "hex", "zlib", "rot13", "undefined", "idna", "punycode"])
def test_declared_codecs_that_are_not_character_sets_fall_back_to_utf8(label: str) -> None:
    raw = f'<?xml version="1.0" encoding="{label}"?><OFX><NAME>CAF\u00c9</NAME></OFX>'.encode()
    assert "CAF\u00c9" in decode_ofx(raw)


@pytest.mark.parametrize("text", ["1" * 16, "-" + "9" * 30, "1" * 27 + ".5"])
def test_ofx_amounts_out_of_range_are_value_errors(text: str) -> None:
    with pytest.raises(ValueError, match="out of range"):
        parse_ofx_amount(text)


@pytest.mark.parametrize(
    "text",
    [
        "<OFX><!-- note --><A>1</A></OFX>",
        "<OFX><?pi x?><A>1</A></OFX>",
        "<OFX><!-- never closed <A>1</A></OFX>",
        "<OFX><? never closed <A>1</A><!-- c --></OFX>",
        "<OFX><!---->x<!--->y-->z<?>?>w</OFX>",
        "<? <!-- ?> --> <!-- <? --> ?>",
    ],
)
def test_strip_markup_gives_what_the_regular_expression_gave(text: str) -> None:
    assert strip_markup(text) == re.sub(r"<!--.*?-->|<\?.*?\?>", "", text, flags=re.DOTALL)


def test_strip_markup_is_linear_when_nothing_closes() -> None:
    hostile = "<OFX>" + "<!--<?" * 50_000  # quadratic for a lazy regular expression
    assert strip_markup(hostile) == hostile
