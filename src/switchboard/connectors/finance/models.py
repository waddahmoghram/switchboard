"""Plain data shared by the parsers, the ledger, and the runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

NAME = "finance"


class FormatError(Exception):
    """A statement file or an alert email could not be read.

    Messages name rows, columns, and elements, never cell values or account numbers, so they are safe
    to return to an assistant and to write to logs.
    """


@dataclass(frozen=True)
class ParsedTransaction:
    """One transaction read from a file or an alert, before it is placed in a ledger account.

    ``amount_cents`` is signed from the account holder's point of view: negative means money left the
    account. ``external_id`` is the bank's own identifier (an OFX ``FITID`` or a CSV id column); when it
    is missing the ledger derives a key from the row content.
    """

    posted: date
    amount_cents: int
    description: str
    memo: str = ""
    external_id: str | None = None
    check_number: str = ""
    kind: str = ""


@dataclass(frozen=True)
class Balance:
    """A balance reported by a statement, as of a date when the file gives one."""

    amount_cents: int
    as_of: date | None
