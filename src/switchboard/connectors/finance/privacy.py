"""Keep account numbers out of storage, tool results, events, and logs.

Only the last four characters of an account number are kept, for display as ``****1234``. To recognize
the same account across imports, the ledger stores a salted SHA-256 of the full number. The salt is
random, generated once, and kept in the database's key-value table. It defeats precomputed tables and
makes hashes differ between installations; it is not encryption, because anyone who can read the
database can also read the salt and try every plausible account number.
"""

from __future__ import annotations

import re
import secrets

from switchboard.connectors.finance.models import NAME
from switchboard.db import Database
from switchboard.util import sha256_hex

SALT_KEY = "identity_salt"
# Nine or more digits, optionally separated by single spaces or dashes: account and card numbers,
# but not eight-digit dates such as 20261105.
_LONG_NUMBER = re.compile(r"(?<!\d)\d(?:[ -]?\d){8,}(?!\d)")


def last4_of(identifier: str) -> str:
    """The last four letters or digits of an account identifier; empty when it is too short to mask."""
    cleaned = "".join(char for char in identifier if char.isalnum())
    return cleaned[-4:] if len(cleaned) > 4 else ""


def mask(last4: str) -> str:
    """Display form of an account: ``****1234``, or ``****`` when the last four are unknown."""
    return f"****{last4}"


def scrub_numbers(text: str) -> str:
    """Mask digit runs that may be account or card numbers: ``TO 123456789`` -> ``TO ****6789``."""
    return _LONG_NUMBER.sub(lambda match: mask(_digits(match.group(0))[-4:]), text)


def identity_hash(salt: str, *parts: str) -> str:
    """Salted SHA-256 over ``parts``, used to recognize an account without storing its number."""
    return sha256_hex("\x1f".join((salt, *parts)))


def identity_salt(db: Database) -> str:
    """Return the installation's salt, creating it on first use."""
    return db.kv_setdefault(NAME, SALT_KEY, secrets.token_hex(16))


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())
