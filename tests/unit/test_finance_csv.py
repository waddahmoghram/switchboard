"""CSV profiles and lenient money parsing."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from switchboard.connectors.finance.csvfile import parse_csv
from switchboard.connectors.finance.models import FormatError
from switchboard.connectors.finance.money import parse_money
from switchboard.connectors.finance.settings import CsvProfile


def profile(**overrides: Any) -> CsvProfile:
    values: dict[str, Any] = {
        "name": "test",
        "date_column": "Date",
        "description_column": "Description",
        "amount_column": "Amount",
    }
    values.update(overrides)
    return CsvProfile(**values)


def rows(raw: bytes, csv_profile: CsvProfile) -> list[tuple[date, int, str]]:
    return [
        (txn.posted, txn.amount_cents, txn.description) for txn in parse_csv(raw, csv_profile).transactions
    ]


def test_amount_column_with_byte_order_mark() -> None:
    raw = b"\xef\xbb\xbfDate,Description,Amount\n2026-11-03,Example Coffee,-3.50\n2026-11-04,Refund,2.00\n"

    assert rows(raw, profile()) == [
        (date(2026, 11, 3), -350, "Example Coffee"),
        (date(2026, 11, 4), 200, "Refund"),
    ]


def test_byte_order_mark_is_ignored_with_plain_utf8_too() -> None:
    raw = b"\xef\xbb\xbfDate,Description,Amount\n2026-11-03,Example Coffee,-3.50\n"

    assert rows(raw, profile(encoding="utf-8")) == [(date(2026, 11, 3), -350, "Example Coffee")]


def test_byte_order_mark_before_a_quoted_header() -> None:
    raw = b'\xef\xbb\xbf"Date","Description","Amount"\n2026-11-03,"Example Coffee",-3.50\n'

    assert rows(raw, profile(encoding="utf-8")) == [(date(2026, 11, 3), -350, "Example Coffee")]


def test_an_absurd_amount_is_a_skipped_row() -> None:
    raw = b"Date,Description,Amount\n2026-11-03,Corrupt," + b"1" * 30 + b"\n2026-11-04,Refund,2.00\n"

    result = parse_csv(raw, profile())

    assert [(txn.description, txn.amount_cents) for txn in result.transactions] == [("Refund", 200)]
    assert result.skipped == 1


def test_debit_and_credit_columns() -> None:
    raw = (
        b"Posted,Payee,Debit,Credit\n"
        b"11/02/2026,EXAMPLE GROCER,45.10,\n"
        b"11/03/2026,EXAMPLE EMPLOYER,,1200.00\n"
        b"11/04/2026,EXAMPLE FEE,-2.00,\n"
    )
    split = profile(
        date_column="Posted",
        description_column="Payee",
        amount_column="",
        debit_column="Debit",
        credit_column="Credit",
        date_formats=("%m/%d/%Y",),
    )

    assert [amount for _, amount, _ in rows(raw, split)] == [-4510, 120000, -200]


def test_invert_sign_for_card_exports_that_show_purchases_as_positive() -> None:
    raw = b"Transaction Date,Description,Amount\n2026-11-01,EXAMPLE BOOKS,25.00\n2026-11-02,PAYMENT,-100.00\n"

    inverted = profile(date_column="transaction date", invert_sign=True)

    assert [amount for _, amount, _ in rows(raw, inverted)] == [-2500, 10000]


def test_several_date_formats_in_one_profile() -> None:
    raw = b"Date,Description,Amount\n11/05/2026,A,-1.00\n2026-11-06,B,-2.00\n07 Nov 2026,C,-3.00\n"

    formats = profile(date_formats=("%m/%d/%Y", "%Y-%m-%d", "%d %b %Y"))

    assert [posted for posted, _, _ in rows(raw, formats)] == [
        date(2026, 11, 5),
        date(2026, 11, 6),
        date(2026, 11, 7),
    ]


def test_semicolons_decimal_comma_and_windows_1252() -> None:
    raw = "Date;Text;Amount\n05.11.2026;Caf\u00e9 Example;-1.234,56\n06.11.2026;Salary;2.500,00\n".encode(
        "cp1252"
    )
    european = profile(
        description_column="Text",
        delimiter=";",
        decimal_mark=",",
        encoding="cp1252",
        date_formats=("%d.%m.%Y",),
    )

    assert rows(raw, european) == [
        (date(2026, 11, 5), -123456, "Caf\u00e9 Example"),
        (date(2026, 11, 6), 250000, "Salary"),
    ]


def test_preamble_rows_and_footer() -> None:
    raw = (
        b"Example Credit Union\n"
        b"Account ending 6789\n"
        b"Date,Description,Amount\n"
        b"2026-11-01,EXAMPLE MARKET,-20.00\n"
        b"Total,,-20.00\n"
    )

    result = parse_csv(raw, profile(skip_rows=2))

    assert [txn.amount_cents for txn in result.transactions] == [-2000]
    assert result.skipped == 1
    assert result.first_problem == "row 5: the date does not match date_format"


def test_bank_id_column_and_memo() -> None:
    raw = b"Date,Description,Amount,Reference,Notes\n2026-11-01,EXAMPLE MARKET,-20.00,R-1,weekly shop\n"

    [txn] = parse_csv(raw, profile(id_column="Reference", memo_column="Notes")).transactions

    assert (txn.external_id, txn.memo) == ("R-1", "weekly shop")


def test_a_file_where_no_row_matches_the_profile_is_an_error() -> None:
    raw = b"Date,Description,Amount\n11/05/2026,A,-1.00\n"

    with pytest.raises(FormatError, match="no row could be read"):
        parse_csv(raw, profile())


def test_missing_columns_are_named() -> None:
    with pytest.raises(FormatError, match="lacks the column"):
        parse_csv(b"When,What,How much\n", profile())


def test_header_only_file_has_no_transactions() -> None:
    result = parse_csv(b"Date,Description,Amount\n", profile())

    assert (result.transactions, result.skipped) == ((), 0)


def test_wrong_encoding_is_reported() -> None:
    with pytest.raises(FormatError, match="not valid utf-8 text"):
        parse_csv(
            "Date,Description,Amount\n2026-11-01,Caf\u00e9,-1.00\n".encode("cp1252"),
            profile(encoding="utf-8"),
        )


@pytest.mark.parametrize(
    ("text", "mark", "cents"),
    [
        ("$1,234.56", ".", 123456),
        ("-$5.00", ".", -500),
        ("$-5.00", ".", -500),
        ("12.34-", ".", -1234),
        ("(12.34)", ".", -1234),
        ("\u221212.34", ".", -1234),
        ("1.234,56", ",", 123456),
        ("0.005", ".", 1),
        ("-0.005", ".", -1),
    ],
)
def test_parse_money(text: str, mark: str, cents: int) -> None:
    assert parse_money(text, mark) == cents


@pytest.mark.parametrize("text", ["", "abc", "1e5", "NaN", "12.34 USD"])
def test_parse_money_rejects_non_numbers(text: str) -> None:
    with pytest.raises(ValueError, match="not a plain decimal"):
        parse_money(text)
