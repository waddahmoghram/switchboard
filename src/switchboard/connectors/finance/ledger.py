"""The local ledger: accounts, transactions, imports, and budget alerts in SQLite.

Dedupe
------
* Whole files: the SHA-256 of the file's bytes, so importing the same file again is a no-op.
* OFX and QFX: the bank's ``FITID``, which the OFX specification requires to be unique within an
  account. It is stored salted and hashed, because some banks build FITIDs from account numbers.
* CSV, and the rare OFX file without FITIDs: a hash of (account, date, amount, description,
  occurrence). *Occurrence* numbers identical rows within one file. Two coffees for the same price on
  the same day are occurrences 0 and 1, so both are kept; importing the same or an overlapping export
  again reproduces both keys, so neither is duplicated.
* Alerts: the email's Message-ID.

Pending matching
----------------
An alert creates a *pending* transaction. A posted transaction and an unmatched pending one are linked
through ``matched_id``, in both directions, when they share an account and an amount and are at most
three days apart; the most similar merchant wins, then the closest date. Linking works in either arrival
order. Linked pending rows are hidden from listings and totals, so each purchase counts once. A pending
alert whose posted amount differs (a restaurant tip, a fuel pre-authorization) stays unmatched and
visible with ``pending: true``.

All SQL here is static text with bound parameters; optional filters use ``(:x IS NULL OR ...)``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from switchboard.connectors.finance.insights import CategoryTotal, Charge
from switchboard.connectors.finance.merchants import similarity
from switchboard.connectors.finance.models import Balance
from switchboard.connectors.finance.money import cents_to_number
from switchboard.connectors.finance.privacy import mask
from switchboard.db import Database, Migration, transaction
from switchboard.util import canonical_json, now_iso, sha256_hex, truncate

MATCH_WINDOW = timedelta(days=3)
_DESCRIPTION_LIMIT = 120

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            """
            CREATE TABLE finance_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT '',
                last4 TEXT NOT NULL DEFAULT '',
                identity_hash TEXT UNIQUE,
                currency TEXT NOT NULL,
                balance_cents INTEGER,
                available_cents INTEGER,
                balance_as_of TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE finance_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                format TEXT NOT NULL CHECK (format IN ('ofx', 'csv')),
                imported_at TEXT NOT NULL,
                accounts INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE finance_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES finance_accounts(id),
                posted_date TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                description TEXT NOT NULL,
                memo TEXT NOT NULL DEFAULT '',
                merchant TEXT NOT NULL,
                category TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                check_number TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL CHECK (source IN ('ofx', 'csv', 'alert')),
                external_id TEXT NOT NULL,
                pending INTEGER NOT NULL DEFAULT 0 CHECK (pending IN (0, 1)),
                matched_id INTEGER REFERENCES finance_transactions(id),
                import_id INTEGER REFERENCES finance_imports(id),
                created_at TEXT NOT NULL,
                UNIQUE (account_id, external_id)
            )
            """,
            "CREATE INDEX finance_transactions_date ON finance_transactions(posted_date)",
            "CREATE INDEX finance_transactions_match "
            "ON finance_transactions(account_id, amount_cents, pending)",
            """
            CREATE TABLE finance_budget_alerts (
                category TEXT NOT NULL,
                month TEXT NOT NULL,
                spent_cents INTEGER NOT NULL,
                limit_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (category, month)
            )
            """,
        ),
    ),
]


AUTO_NAME_PREFIX = "account-"


def auto_account_name(last4: str, identity: str | None) -> str:
    """Name for an account the configuration does not mention: ``account-1234``."""
    if last4:
        return f"{AUTO_NAME_PREFIX}{last4}"
    return f"{AUTO_NAME_PREFIX}{identity[:6]}" if identity else "alerts"


def is_auto_name(name: str, last4: str) -> bool:
    """True for ``account-1234`` and its twin ``account-1234-<hash>``, the names imports invent."""
    base = auto_account_name(last4, None)
    return bool(last4) and (name == base or name.startswith(base + "-"))


@dataclass(frozen=True)
class AccountRef:
    """Which ledger account a batch belongs to.

    ``identity_hash`` is the salted hash of the full account number when the file has one (OFX). An
    ``explicit`` name comes from the configuration or the command line and renames an account that an
    earlier import named automatically.
    """

    name: str
    currency: str
    kind: str = ""
    last4: str = ""
    identity_hash: str | None = None
    explicit: bool = False


@dataclass(frozen=True)
class NewTransaction:
    """A transaction ready for the ledger. ``external_id`` None means: derive a key from the row."""

    posted: date
    amount_cents: int
    currency: str
    description: str
    merchant: str
    category: str
    source: str
    external_id: str | None = None
    memo: str = ""
    kind: str = ""
    check_number: str = ""
    pending: bool = False


@dataclass(frozen=True)
class AccountBatch:
    account: AccountRef
    transactions: tuple[NewTransaction, ...]
    ledger_balance: Balance | None = None
    available_balance: Balance | None = None


@dataclass(frozen=True)
class ImportOutcome:
    duplicate_file: bool
    new_ids: tuple[int, ...] = ()
    duplicates: int = 0
    matched_ids: tuple[int, ...] = ()
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransactionQuery:
    """Filters for :meth:`Ledger.transactions`; amounts are signed cents, dates inclusive."""

    start: date | None = None
    end: date | None = None
    account: str | None = None
    category: str | None = None
    merchant_contains: str | None = None
    min_cents: int | None = None
    max_cents: int | None = None
    pending: bool | None = None
    limit: int = 50


@dataclass(frozen=True)
class TransactionRow:
    id: int
    account: str
    posted: date
    amount_cents: int
    currency: str
    description: str
    merchant: str
    category: str
    pending: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "date": self.posted.isoformat(),
            "amount": cents_to_number(self.amount_cents),
            "currency": self.currency,
            "merchant": self.merchant,
            "description": truncate(self.description, _DESCRIPTION_LIMIT),
            "category": self.category,
            "pending": self.pending,
            "source": self.source,
        }

    def event_payload(self) -> dict[str, Any]:
        """The ``finance.transaction`` payload: amount is a number rounded to cents, negative = money out."""
        return {
            "id": self.id,
            "account": self.account,
            "date": self.posted.isoformat(),
            "amount": cents_to_number(self.amount_cents),
            "currency": self.currency,
            "merchant": self.merchant,
            "pending": self.pending,
            "source": self.source,
            "category": self.category,
        }


@dataclass(frozen=True)
class AccountRow:
    name: str
    kind: str
    last4: str
    currency: str
    balance_cents: int | None
    available_cents: int | None
    balance_as_of: str | None
    transactions: int
    last_activity: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "number": mask(self.last4),
            "kind": self.kind or "other",
            "currency": self.currency,
            "balance": _number_or_none(self.balance_cents),
            "available_balance": _number_or_none(self.available_cents),
            "balance_as_of": self.balance_as_of,
            "transactions": self.transactions,
            "last_activity": self.last_activity,
        }


@dataclass(frozen=True)
class Candidate:
    """An unmatched transaction that might be the other half of a pending/posted pair."""

    id: int
    posted: date
    merchant: str


def row_keys(account_id: int, transactions: Sequence[NewTransaction]) -> list[str]:
    """Dedupe keys: the bank's id when there is one, else a hash of the row and its occurrence."""
    seen: dict[tuple[str, int, str], int] = {}
    keys: list[str] = []
    for txn in transactions:
        if txn.external_id:
            keys.append(txn.external_id)
            continue
        identity = (txn.posted.isoformat(), txn.amount_cents, " ".join(txn.description.casefold().split()))
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        keys.append("row:" + sha256_hex(canonical_json([account_id, *identity, occurrence]))[:40])
    return keys


def best_match(merchant: str, posted: date, candidates: Sequence[Candidate]) -> int:
    """The candidate with the most similar merchant, then the closest date, then the oldest id."""

    def rank(candidate: Candidate) -> tuple[float, int, int]:
        return (
            -similarity(merchant, candidate.merchant),
            abs((candidate.posted - posted).days),
            candidate.id,
        )

    return min(candidates, key=rank).id


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- writes

    def record_import(
        self,
        *,
        digest: str,
        name: str,
        fmt: str,
        batches: Sequence[AccountBatch],
        skipped: int,
        today: date,
    ) -> ImportOutcome:
        """Store one file's batches in a single transaction; a file seen before changes nothing."""
        with self.db.session() as conn, transaction(conn):
            seen = conn.execute("SELECT 1 FROM finance_imports WHERE sha256 = ?", (digest,)).fetchone()
            if seen is not None:
                return ImportOutcome(duplicate_file=True)
            cursor = conn.execute(
                "INSERT INTO finance_imports (sha256, name, format, imported_at, skipped_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest, name, fmt, now_iso(), skipped),
            )
            import_id = _row_id(cursor)
            new_ids: list[int] = []
            matched: list[int] = []
            duplicates = 0
            names: list[str] = []
            for batch in batches:
                account_id, account_name = self._account(conn, batch.account)
                names.append(account_name)
                self._update_balance(conn, account_id, batch, today)
                ids, batch_duplicates = self._insert(conn, account_id, batch.transactions, import_id)
                duplicates += batch_duplicates
                for txn_id in ids:
                    if self._link(conn, txn_id) is not None:
                        matched.append(txn_id)  # noqa: PERF401 - _link writes rows; not a pure filter
                new_ids.extend(ids)
            conn.execute(
                "UPDATE finance_imports SET accounts = ?, new_count = ?, duplicate_count = ? WHERE id = ?",
                (len(batches), len(new_ids), duplicates, import_id),
            )
        return ImportOutcome(False, tuple(new_ids), duplicates, tuple(matched), tuple(names))

    def add_alert(self, account: AccountRef, txn: NewTransaction) -> tuple[int | None, int | None]:
        """Store a pending alert: (its id, or None when already stored; the posted id it matched)."""
        with self.db.session() as conn, transaction(conn):
            account_id, _ = self._account(conn, account)
            ids, _ = self._insert(conn, account_id, (txn,), None)
            if not ids:
                return None, None
            return ids[0], self._link(conn, ids[0])

    def record_budget_alert(self, category: str, month: str, spent_cents: int, limit_cents: int) -> bool:
        """Remember that a budget alert fired; False when it already had this month."""
        with self.db.session() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO finance_budget_alerts "
                "(category, month, spent_cents, limit_cents, created_at) VALUES (?, ?, ?, ?, ?)",
                (category, month, spent_cents, limit_cents, now_iso()),
            )
            return cursor.rowcount == 1

    def recategorize(self, categorize: Callable[[str, str, str], str]) -> int:
        """Apply ``categorize(merchant, description, memo)`` to every row; return how many changed."""
        changed = 0
        with self.db.session() as conn, transaction(conn):
            rows = conn.execute(
                "SELECT id, merchant, description, memo, category FROM finance_transactions"
            ).fetchall()
            for row in rows:
                category = categorize(str(row["merchant"]), str(row["description"]), str(row["memo"]))
                if category != row["category"]:
                    conn.execute(
                        "UPDATE finance_transactions SET category = ? WHERE id = ?", (category, row["id"])
                    )
                    changed += 1
        return changed

    # ---- reads

    def accounts(self) -> list[AccountRow]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT a.name, a.kind, a.last4, a.currency, a.balance_cents, a.available_cents, "
                "a.balance_as_of, COUNT(t.id) AS transactions, MAX(t.posted_date) AS last_activity "
                "FROM finance_accounts a LEFT JOIN finance_transactions t "
                "ON t.account_id = a.id AND (t.pending = 0 OR t.matched_id IS NULL) "
                "GROUP BY a.id ORDER BY a.name"
            ).fetchall()
        return [
            AccountRow(
                name=str(row["name"]),
                kind=str(row["kind"]),
                last4=str(row["last4"]),
                currency=str(row["currency"]),
                balance_cents=row["balance_cents"],
                available_cents=row["available_cents"],
                balance_as_of=row["balance_as_of"],
                transactions=int(row["transactions"]),
                last_activity=row["last_activity"],
            )
            for row in rows
        ]

    def account_names_with_last4(self, last4: str) -> list[str]:
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT name FROM finance_accounts WHERE last4 = ? ORDER BY id", (last4,)
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def transactions(self, query: TransactionQuery) -> tuple[list[TransactionRow], bool]:
        """Matching rows, newest first, and whether more rows matched than ``query.limit``."""
        params = {
            "start_date": None if query.start is None else query.start.isoformat(),
            "end_date": None if query.end is None else query.end.isoformat(),
            "account": query.account,
            "category": query.category,
            "merchant": query.merchant_contains,
            "min_cents": query.min_cents,
            "max_cents": query.max_cents,
            "pending": None if query.pending is None else int(query.pending),
            "row_limit": query.limit + 1,
        }
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT t.id, a.name AS account, t.posted_date, t.amount_cents, t.currency, t.description, "
                "t.merchant, t.category, t.pending, t.source "
                "FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id "
                "WHERE (t.pending = 0 OR t.matched_id IS NULL) "
                "AND (:start_date IS NULL OR t.posted_date >= :start_date) "
                "AND (:end_date IS NULL OR t.posted_date <= :end_date) "
                "AND (:account IS NULL OR a.name = :account) "
                "AND (:category IS NULL OR t.category = :category) "
                "AND (:merchant IS NULL OR instr(lower(t.merchant), lower(:merchant)) > 0 "
                "OR instr(lower(t.description), lower(:merchant)) > 0) "
                "AND (:min_cents IS NULL OR t.amount_cents >= :min_cents) "
                "AND (:max_cents IS NULL OR t.amount_cents <= :max_cents) "
                "AND (:pending IS NULL OR t.pending = :pending) "
                "ORDER BY t.posted_date DESC, t.id DESC LIMIT :row_limit",
                params,
            ).fetchall()
        found = [_transaction_row(row) for row in rows]
        return found[: query.limit], len(found) > query.limit

    def transaction(self, txn_id: int) -> TransactionRow | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT t.id, a.name AS account, t.posted_date, t.amount_cents, t.currency, t.description, "
                "t.merchant, t.category, t.pending, t.source "
                "FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id WHERE t.id = ?",
                (txn_id,),
            ).fetchone()
        return None if row is None else _transaction_row(row)

    def category_totals(self, start: date, end: date, currency: str) -> list[CategoryTotal]:
        """Totals per category for ``start <= date < end`` in one currency; linked alerts excluded."""
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT t.category, "
                "SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) AS inflow, "
                "SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END) AS outflow, "
                "COUNT(*) AS n, SUM(t.pending) AS pending "
                "FROM finance_transactions t "
                "WHERE t.posted_date >= ? AND t.posted_date < ? AND t.currency = ? "
                "AND (t.pending = 0 OR t.matched_id IS NULL) "
                "GROUP BY t.category ORDER BY outflow DESC, t.category",
                (start.isoformat(), end.isoformat(), currency),
            ).fetchall()
        return [
            CategoryTotal(
                category=str(row["category"]),
                inflow_cents=int(row["inflow"]),
                outflow_cents=int(row["outflow"]),
                count=int(row["n"]),
                pending=int(row["pending"]),
            )
            for row in rows
        ]

    def charges(self, since: date) -> list[Charge]:
        """Money out since ``since`` (inclusive), oldest first; linked alerts excluded."""
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT t.id, a.name AS account, t.merchant, t.category, t.posted_date, t.amount_cents, "
                "t.currency FROM finance_transactions t JOIN finance_accounts a ON a.id = t.account_id "
                "WHERE t.posted_date >= ? AND t.amount_cents < 0 AND (t.pending = 0 OR t.matched_id IS NULL) "
                "ORDER BY t.posted_date, t.id",
                (since.isoformat(),),
            ).fetchall()
        return [
            Charge(
                id=int(row["id"]),
                account=str(row["account"]),
                merchant=str(row["merchant"]),
                category=str(row["category"]),
                posted=date.fromisoformat(str(row["posted_date"])),
                amount_cents=int(row["amount_cents"]),
                currency=str(row["currency"]),
            )
            for row in rows
        ]

    # ---- helpers

    def _account(self, conn: sqlite3.Connection, ref: AccountRef) -> tuple[int, str]:
        """Find or create the account for ``ref``: by identity hash, by name, then by adopting the one
        automatically named account that ends in the same digits."""
        if ref.identity_hash:
            row = conn.execute(
                "SELECT id, name FROM finance_accounts WHERE identity_hash = ?", (ref.identity_hash,)
            ).fetchone()
            if row is not None:
                return self._maybe_rename(conn, int(row["id"]), str(row["name"]), ref)
        name = ref.name
        row = _by_name(conn, name)
        if row is not None and ref.identity_hash and row["identity_hash"] is not None:
            # The name belongs to another account (two accounts ending in the same digits): keep them apart.
            name = f"{ref.name}-{ref.identity_hash[:6]}"
            row = _by_name(conn, name)
        if row is None:
            row = _auto_named_twin(conn, ref)
            if row is not None:
                _, name = self._maybe_rename(conn, int(row["id"]), str(row["name"]), ref)
        if row is None:
            return self._insert_account(conn, ref, name), name
        conn.execute(
            "UPDATE finance_accounts SET identity_hash = COALESCE(identity_hash, ?), "
            "last4 = CASE WHEN last4 = '' THEN ? ELSE last4 END, "
            "kind = CASE WHEN kind = '' THEN ? ELSE kind END, updated_at = ? WHERE id = ?",
            (ref.identity_hash, ref.last4, ref.kind, now_iso(), row["id"]),
        )
        return int(row["id"]), name

    def _maybe_rename(
        self, conn: sqlite3.Connection, account_id: int, current: str, ref: AccountRef
    ) -> tuple[int, str]:
        if ref.explicit and ref.name != current and _by_name(conn, ref.name) is None:
            conn.execute(
                "UPDATE finance_accounts SET name = ?, updated_at = ? WHERE id = ?",
                (ref.name, now_iso(), account_id),
            )
            return account_id, ref.name
        return account_id, current

    def _insert_account(self, conn: sqlite3.Connection, ref: AccountRef, name: str) -> int:
        stamp = now_iso()
        cursor = conn.execute(
            "INSERT INTO finance_accounts "
            "(name, kind, last4, identity_hash, currency, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, ref.kind, ref.last4, ref.identity_hash, ref.currency, stamp, stamp),
        )
        return _row_id(cursor)

    def _update_balance(
        self, conn: sqlite3.Connection, account_id: int, batch: AccountBatch, today: date
    ) -> None:
        """Keep the newest statement balance; an older statement imported later does not overwrite it."""
        if batch.ledger_balance is None:
            return
        as_of = (batch.ledger_balance.as_of or today).isoformat()
        available = None if batch.available_balance is None else batch.available_balance.amount_cents
        conn.execute(
            "UPDATE finance_accounts SET balance_cents = ?, available_cents = ?, balance_as_of = ?, "
            "updated_at = ? WHERE id = ? AND (balance_as_of IS NULL OR balance_as_of <= ?)",
            (batch.ledger_balance.amount_cents, available, as_of, now_iso(), account_id, as_of),
        )

    def _insert(
        self,
        conn: sqlite3.Connection,
        account_id: int,
        transactions: Sequence[NewTransaction],
        import_id: int | None,
    ) -> tuple[list[int], int]:
        """Insert new rows; return their ids and the number of duplicates skipped."""
        new_ids: list[int] = []
        duplicates = 0
        stamp = now_iso()
        for txn, key in zip(transactions, row_keys(account_id, transactions), strict=True):
            cursor = conn.execute(
                "INSERT OR IGNORE INTO finance_transactions "
                "(account_id, posted_date, amount_cents, currency, description, memo, merchant, category, "
                "kind, check_number, source, external_id, pending, import_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    txn.posted.isoformat(),
                    txn.amount_cents,
                    txn.currency,
                    txn.description,
                    txn.memo,
                    txn.merchant,
                    txn.category,
                    txn.kind,
                    txn.check_number,
                    txn.source,
                    key,
                    int(txn.pending),
                    import_id,
                    stamp,
                ),
            )
            if cursor.rowcount == 1:
                new_ids.append(_row_id(cursor))
            else:
                duplicates += 1
        return new_ids, duplicates

    def _link(self, conn: sqlite3.Connection, txn_id: int) -> int | None:
        """Link ``txn_id`` with its pending or posted counterpart, if one exists; return that id."""
        row = conn.execute(
            "SELECT account_id, posted_date, amount_cents, merchant, pending FROM finance_transactions "
            "WHERE id = ?",
            (txn_id,),
        ).fetchone()
        posted = date.fromisoformat(str(row["posted_date"]))
        rows = conn.execute(
            "SELECT id, posted_date, merchant FROM finance_transactions "
            "WHERE account_id = ? AND amount_cents = ? AND pending = ? AND matched_id IS NULL AND id != ? "
            "AND posted_date BETWEEN ? AND ?",
            (
                row["account_id"],
                row["amount_cents"],
                0 if row["pending"] else 1,
                txn_id,
                (posted - MATCH_WINDOW).isoformat(),
                (posted + MATCH_WINDOW).isoformat(),
            ),
        ).fetchall()
        if not rows:
            return None
        candidates = [
            Candidate(int(item["id"]), date.fromisoformat(str(item["posted_date"])), str(item["merchant"]))
            for item in rows
        ]
        partner = best_match(str(row["merchant"]), posted, candidates)
        conn.execute("UPDATE finance_transactions SET matched_id = ? WHERE id = ?", (partner, txn_id))
        conn.execute("UPDATE finance_transactions SET matched_id = ? WHERE id = ?", (txn_id, partner))
        return partner


def _auto_named_twin(conn: sqlite3.Connection, ref: AccountRef) -> sqlite3.Row | None:
    """The account an import named ``account-<last4>`` before the configuration named it.

    Alerts carry only the last four digits and statements the full number, so whichever arrives first
    creates the account under an automatic name. When the configuration later names it, the other source
    must land in that same account; a second one would count every alerted purchase twice. Two different
    identity hashes mean two real accounts, which stay apart.
    """
    if not ref.explicit or not ref.last4:
        return None
    rows = conn.execute(
        "SELECT id, name, identity_hash FROM finance_accounts WHERE last4 = ? ORDER BY id", (ref.last4,)
    ).fetchall()
    if len(rows) != 1 or not is_auto_name(str(rows[0]["name"]), ref.last4):
        return None
    if ref.identity_hash and rows[0]["identity_hash"] is not None:
        return None
    row: sqlite3.Row = rows[0]
    return row


def _by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, identity_hash FROM finance_accounts WHERE name = ?", (name,)
    ).fetchone()
    return row


def _row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always sets it after an INSERT
        raise RuntimeError("SQLite did not report the new row id")
    return int(cursor.lastrowid)


def _transaction_row(row: sqlite3.Row) -> TransactionRow:
    return TransactionRow(
        id=int(row["id"]),
        account=str(row["account"]),
        posted=date.fromisoformat(str(row["posted_date"])),
        amount_cents=int(row["amount_cents"]),
        currency=str(row["currency"]),
        description=str(row["description"]),
        merchant=str(row["merchant"]),
        category=str(row["category"]),
        pending=bool(row["pending"]),
        source=str(row["source"]),
    )


def _number_or_none(cents: int | None) -> float | None:
    return None if cents is None else cents_to_number(cents)
