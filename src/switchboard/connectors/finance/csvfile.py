"""Read bank CSV exports with a configured profile.

Every bank lays out its export differently, so a profile names the columns and formats:

* ``skip_rows`` rows of preamble (account name, export date) come before the header row.
* The header row names the columns, compared without regard to case. A byte order mark before it is
  ignored even when the profile's encoding is plain ``utf-8`` instead of the default ``utf-8-sig``.
* The amount is one signed column, or a debit column and a credit column. Debit and credit cells are
  read as magnitudes, because some banks print debits as negative numbers and others as positive.
* ``invert_sign`` flips amounts for exports that show purchases as positive numbers (common for cards).
* Rows whose date or amount cannot be read (a "Total" footer, a disclaimer) are skipped and counted. A
  file in which no row can be read is an error, because that usually means the profile is wrong.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from switchboard.connectors.finance.models import FormatError, ParsedTransaction
from switchboard.connectors.finance.money import parse_money
from switchboard.connectors.finance.settings import CsvProfile


@dataclass(frozen=True)
class CsvResult:
    transactions: tuple[ParsedTransaction, ...]
    skipped: int
    first_problem: str = ""


def parse_csv(raw: bytes, profile: CsvProfile) -> CsvResult:
    """Transactions from one CSV export, in file order."""
    try:
        # Drop a byte order mark before the csv module sees it: '\ufeff"Date"' is not a quoted field.
        text = raw.decode(profile.encoding).removeprefix("\ufeff")
    except (UnicodeError, LookupError) as exc:
        raise FormatError(f"the file is not valid {profile.encoding} text") from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=profile.delimiter))
    except csv.Error as exc:
        raise FormatError(f"the file is not valid CSV: {exc}") from exc
    if len(rows) <= profile.skip_rows:
        raise FormatError("the file has no header row after skip_rows")
    header = [cell.strip() for cell in rows[profile.skip_rows]]
    columns = _column_indexes(header, profile)
    transactions: list[ParsedTransaction] = []
    skipped = 0
    first_problem = ""
    # Row numbers count from 1 and include the preamble and header, like a spreadsheet's.
    for number, row in enumerate(rows[profile.skip_rows + 1 :], start=profile.skip_rows + 2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            transactions.append(_transaction(row, columns, profile))
        except FormatError as exc:
            skipped += 1
            first_problem = first_problem or f"row {number}: {exc}"
    if skipped and not transactions:
        raise FormatError(f"no row could be read ({first_problem}); check the profile's columns and formats")
    return CsvResult(tuple(transactions), skipped, first_problem)


def _column_indexes(header: Sequence[str], profile: CsvProfile) -> dict[str, int]:
    wanted = {
        "date": profile.date_column,
        "description": profile.description_column,
        "amount": profile.amount_column,
        "debit": profile.debit_column,
        "credit": profile.credit_column,
        "memo": profile.memo_column,
        "id": profile.id_column,
    }
    # Reversed so that the first of two identically named columns wins.
    positions = {name.casefold(): index for index, name in reversed(list(enumerate(header)))}
    missing = [column for column in wanted.values() if column and column.casefold() not in positions]
    if missing:
        raise FormatError(f"the header row lacks the column(s) {', '.join(missing)}")
    return {role: positions[column.casefold()] for role, column in wanted.items() if column}


def _transaction(row: Sequence[str], columns: Mapping[str, int], profile: CsvProfile) -> ParsedTransaction:
    def cell(role: str) -> str:
        index = columns.get(role)
        return row[index].strip() if index is not None and index < len(row) else ""

    amount = _amount(cell, profile)
    description = cell("description")
    memo = cell("memo")
    return ParsedTransaction(
        posted=_date(cell("date"), profile.date_formats),
        amount_cents=-amount if profile.invert_sign else amount,
        description=description or memo or "transaction",
        memo=memo if description else "",
        external_id=cell("id") or None,
    )


def _date(text: str, formats: Sequence[str]) -> date:
    for fmt in formats:
        parsed = strptime_date(text, fmt)
        if parsed is not None:
            return parsed
    raise FormatError("the date does not match date_format")


def strptime_date(text: str, fmt: str) -> date | None:
    """``datetime.strptime(text, fmt).date()``, or None when the text does not match."""
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def _amount(cell: Callable[[str], str], profile: CsvProfile) -> int:
    try:
        if profile.amount_column:
            text = cell("amount")
            if not text:
                raise FormatError("the amount is empty")
            return parse_money(text, profile.decimal_mark)
        debit, credit = cell("debit"), cell("credit")
        if not debit and not credit:
            raise FormatError("both the debit and the credit are empty")
        money_out = abs(parse_money(debit, profile.decimal_mark)) if debit else 0
        money_in = abs(parse_money(credit, profile.decimal_mark)) if credit else 0
        return money_in - money_out
    except ValueError as exc:
        raise FormatError("the amount is not a number") from exc
