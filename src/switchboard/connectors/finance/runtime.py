"""Wire the finance pieces together: imports, alert emails, events, and budgets.

Event volume
------------
One import (a ``finance_import_inbox`` call or one ``switchboard finance import``) emits at most
:data:`MAX_EVENTS_PER_IMPORT` ``finance.transaction`` events, for the newest new transactions. A first
import of a year of history therefore does not flood your rules. A posted transaction that confirms an
earlier pending alert emits nothing, because the alert already announced that purchase.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from switchboard.connectors.finance.alerts import AlertMatch, match_alert
from switchboard.connectors.finance.csvfile import parse_csv
from switchboard.connectors.finance.inbox import (
    FAILED,
    PROCESSED,
    inbox_problems,
    move_to,
    open_inbox,
    read_file,
    scan,
    subdirectory,
)
from switchboard.connectors.finance.insights import (
    build_summary,
    detect_recurring,
    exceeded_budgets,
    month_bounds,
)
from switchboard.connectors.finance.ledger import (
    AccountBatch,
    AccountRef,
    Ledger,
    NewTransaction,
    TransactionQuery,
    auto_account_name,
)
from switchboard.connectors.finance.merchants import categorize, normalize_merchant
from switchboard.connectors.finance.models import NAME, FormatError, ParsedTransaction
from switchboard.connectors.finance.money import cents_to_number
from switchboard.connectors.finance.ofx import OfxStatement, is_ofx, parse_ofx
from switchboard.connectors.finance.privacy import identity_hash, identity_salt, last4_of, scrub_numbers
from switchboard.connectors.finance.settings import NAME_PATTERN, CsvProfile, load_settings
from switchboard.context import AppContext
from switchboard.errors import ToolError
from switchboard.events import Event
from switchboard.util import glob_any, sha256_hex

log = logging.getLogger(__name__)

MAX_EVENTS_PER_IMPORT = 20
MAX_FILES_PER_RUN = 50
RECURRING_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class FileResult:
    """What happened to one file. ``event_ids`` are new transactions that should announce themselves."""

    name: str
    status: str
    format: str = ""
    accounts: tuple[str, ...] = ()
    new: int = 0
    duplicates: int = 0
    matched_pending: int = 0
    skipped_rows: int = 0
    error: str = ""
    event_ids: tuple[int, ...] = field(default=(), repr=False)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"file": self.name, "status": self.status}
        if self.format:
            result["format"] = self.format
        if self.status == "imported":
            result.update(
                accounts=list(self.accounts),
                new=self.new,
                duplicates=self.duplicates,
                matched_pending=self.matched_pending,
                skipped_rows=self.skipped_rows,
            )
        if self.error:
            result["error"] = self.error
        return result


def detect_format(name: str, raw: bytes) -> str | None:
    """``"ofx"`` for .ofx and .qfx files, ``"csv"`` for .csv files; content decides other names."""
    suffix = Path(name).suffix.lower()
    if suffix in {".ofx", ".qfx"}:
        return "ofx"
    if suffix == ".csv":
        return "csv"
    return "ofx" if is_ofx(raw) else None


class FinanceRuntime:
    """Everything the tools, the alert subscriber, and the command line share."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.settings = load_settings(ctx.connector_config(NAME))
        self.ledger = Ledger(ctx.db)
        self._salt: str | None = None

    def today(self) -> date:
        """Today in the configured time zone, or in this computer's."""
        return datetime.now(self.settings.zone).date()

    def salt(self) -> str:
        if self._salt is None:
            self._salt = identity_salt(self.ctx.db)
        return self._salt

    # ---- imports

    def import_inbox(self, *, today: date) -> dict[str, Any]:
        """Import every new file in ``import_dir``; never reads any other path."""
        if self.settings.import_dir is None:
            raise ToolError("set import_dir in [connectors.finance] to use the import inbox")
        inbox = open_inbox(self.settings.import_dir)
        files, ignored = scan(inbox)
        results = [FileResult(scrub_numbers(name), "skipped", error=reason) for name, reason in ignored]
        for path in files[:MAX_FILES_PER_RUN]:
            results.append(self._import_inbox_file(inbox, path, today))
        summary = self._finish(results, today)
        if len(files) > MAX_FILES_PER_RUN:
            summary["files_remaining"] = len(files) - MAX_FILES_PER_RUN
        return summary

    def import_path(
        self, path: Path, *, today: date, account: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Import one file named on the command line; the file stays where it is."""
        if account is not None and not re.match(NAME_PATTERN, account):
            raise ToolError("account names use letters, digits, spaces, '.', '_', or '-' (64 at most)")
        try:
            raw = read_file(path, follow_symlinks=True)
            result = self.import_bytes(path.name, raw, today=today, account=account, profile=profile)
        except FormatError as exc:
            raise ToolError(f"{scrub_numbers(path.name)}: {exc}") from exc
        return self._finish([result], today)

    def import_bytes(
        self,
        name: str,
        raw: bytes,
        *,
        today: date,
        account: str | None = None,
        profile: str | None = None,
    ) -> FileResult:
        """Parse and store one file's content; raise FormatError when it cannot be read."""
        display = scrub_numbers(name)
        fmt = detect_format(name, raw)
        if fmt == "ofx":
            batches, skipped = self._ofx_batches(raw, account), 0
        elif fmt == "csv":
            batches, skipped = self._csv_batches(name, raw, account, profile)
        else:
            raise FormatError("unsupported file type; use .ofx, .qfx, or .csv")
        outcome = self.ledger.record_import(
            digest=sha256_hex(raw), name=display, fmt=fmt, batches=batches, skipped=skipped, today=today
        )
        if outcome.duplicate_file:
            return FileResult(display, "duplicate", fmt)
        matched = set(outcome.matched_ids)
        return FileResult(
            name=display,
            status="imported",
            format=fmt,
            accounts=outcome.accounts,
            new=len(outcome.new_ids),
            duplicates=outcome.duplicates,
            matched_pending=len(matched),
            skipped_rows=skipped,
            event_ids=tuple(txn_id for txn_id in outcome.new_ids if txn_id not in matched),
        )

    def _import_inbox_file(self, inbox: Path, path: Path, today: date) -> FileResult:
        """Import one inbox file. A bad file goes to ``failed/``; an unexpected error (a locked database,
        a parser bug) leaves it in place for the next run and never stops the files after it."""
        name = scrub_numbers(path.name)
        try:
            result = self.import_bytes(path.name, read_file(path), today=today)
        except FormatError as exc:
            move_to(path, subdirectory(inbox, FAILED))
            return FileResult(name, "failed", error=str(exc))
        except Exception as exc:
            # Only the type is logged at error level: a message could quote an account number from the file.
            log.error("finance import of %s failed with %s; it stays in the inbox", name, type(exc).__name__)
            log.debug("finance import of %s failed", name, exc_info=True)
            return FileResult(name, "error", error=f"unexpected {type(exc).__name__}; it will be retried")
        move_to(path, subdirectory(inbox, PROCESSED))
        return result

    def _finish(self, results: Sequence[FileResult], today: date) -> dict[str, Any]:
        emitted, held_back = self.emit_transactions([txn_id for item in results for txn_id in item.event_ids])
        imported = any(item.status == "imported" for item in results)
        return {
            "files": [item.to_dict() for item in results],
            "new_transactions": sum(item.new for item in results),
            "events_emitted": emitted,
            "events_not_emitted": held_back,
            "budget_alerts": self.check_budgets(today) if imported else [],
        }

    def _ofx_batches(self, raw: bytes, account: str | None) -> list[AccountBatch]:
        statements = parse_ofx(raw)
        if account and len(statements) > 1:
            raise FormatError(
                "this file holds several accounts; map them with last4 in [connectors.finance.accounts]"
            )
        return [self._ofx_batch(statement, account) for statement in statements]

    def _ofx_batch(self, statement: OfxStatement, account: str | None) -> AccountBatch:
        last4 = last4_of(statement.account_number)
        identity = identity_hash(
            self.salt(), "ofx", statement.bank_id.strip(), statement.account_number.strip()
        )
        configured = self.settings.account_for_last4(last4)
        kind = "credit_card" if statement.kind == "credit_card" else statement.account_type.lower()
        ref = AccountRef(
            name=account or (configured.name if configured else auto_account_name(last4, identity)),
            currency=statement.currency or self.settings.currency,
            kind=(configured.kind if configured else "") or kind,
            last4=last4,
            identity_hash=identity,
            explicit=bool(account or configured),
        )
        transactions = tuple(
            self._new_transaction(item, currency=ref.currency, source="ofx")
            for item in statement.transactions
        )
        return AccountBatch(ref, transactions, statement.ledger_balance, statement.available_balance)

    def _csv_batches(
        self, name: str, raw: bytes, account: str | None, profile_name: str | None
    ) -> tuple[list[AccountBatch], int]:
        profile = self._profile_for(name, profile_name)
        parsed = parse_csv(raw, profile)
        account_name = account or self.settings.account_for_profile(profile)
        configured = self.settings.accounts.get(account_name)
        ref = AccountRef(
            name=account_name,
            currency=profile.currency or self.settings.currency,
            kind=configured.kind if configured else "",
            last4=configured.last4 if configured else "",
            explicit=True,
        )
        transactions = tuple(
            self._new_transaction(item, currency=ref.currency, source="csv") for item in parsed.transactions
        )
        return [AccountBatch(ref, transactions)], parsed.skipped

    def _profile_for(self, filename: str, requested: str | None) -> CsvProfile:
        """The profile named on the command line, else the one whose ``files`` glob matches the name,
        else the only profile without a ``files`` glob."""
        profiles = self.settings.csv_profiles
        if requested:
            if requested not in profiles:
                raise FormatError(f"there is no CSV profile named {requested!r}")
            return profiles[requested]
        matching = [
            profile for profile in profiles.values() if profile.files and glob_any(filename, [profile.files])
        ]
        if len(matching) == 1:
            return matching[0]
        fallback = [profile for profile in profiles.values() if not profile.files]
        if not matching and len(fallback) == 1:
            return fallback[0]
        raise FormatError(
            "cannot tell which CSV profile fits this file; give each profile a files glob such as "
            'files = "example-cu-*.csv"'
        )

    def _new_transaction(self, item: ParsedTransaction, *, currency: str, source: str) -> NewTransaction:
        description = scrub_numbers(item.description)
        memo = scrub_numbers(item.memo)
        merchant = normalize_merchant(description)
        external_id = None
        if item.external_id:
            external_id = f"{source}:{identity_hash(self.salt(), source, item.external_id)[:40]}"
        return NewTransaction(
            posted=item.posted,
            amount_cents=item.amount_cents,
            currency=currency,
            description=description,
            merchant=merchant,
            category=categorize(self.settings.categories, merchant, description, memo),
            source=source,
            external_id=external_id,
            memo=memo,
            kind=item.kind,
            check_number=item.check_number,
        )

    # ---- alerts

    def on_mail_event(self, event: Event) -> None:
        """``mail.received`` subscriber."""
        self.handle_mail(event.payload, today=self.today())

    def handle_mail(self, payload: Mapping[str, Any], *, today: date) -> int | None:
        """Record a recognized alert email as a pending transaction; return its id when it is new."""
        if not self.settings.alert_rules:
            return None
        try:
            found = match_alert(self.settings.alert_rules, payload, today=today, zone=self.settings.zone)
        except FormatError as exc:
            log.warning("finance: %s", exc)
            return None
        if found is None:
            return None
        ref = self._alert_account(found)
        description = scrub_numbers(found.merchant) or found.rule
        merchant = normalize_merchant(description)
        txn = NewTransaction(
            posted=found.posted,
            amount_cents=found.amount_cents,
            currency=found.currency or ref.currency,
            description=description,
            merchant=merchant,
            category=categorize(self.settings.categories, merchant, description),
            source="alert",
            external_id=f"alert:{found.message_key}",
            pending=True,
        )
        txn_id, partner = self.ledger.add_alert(ref, txn)
        if txn_id is None:
            return None
        if partner is None:
            self.emit_transactions([txn_id])
        self.check_budgets(today)
        return txn_id

    def _alert_account(self, found: AlertMatch) -> AccountRef:
        """The rule's account, else the account ending in the alert's last four digits."""
        currency = found.currency or self.settings.currency
        if found.account:
            configured = self.settings.accounts.get(found.account)
            return AccountRef(
                name=found.account,
                currency=currency,
                kind=configured.kind if configured else "",
                last4=(configured.last4 if configured else "") or found.last4,
                explicit=True,
            )
        configured = self.settings.account_for_last4(found.last4)
        if configured is not None:
            return AccountRef(configured.name, currency, configured.kind, found.last4, explicit=True)
        existing = self.ledger.account_names_with_last4(found.last4) if found.last4 else []
        name = existing[0] if len(existing) == 1 else auto_account_name(found.last4, None)
        return AccountRef(name, currency, last4=found.last4)

    # ---- events and budgets

    def emit_transactions(self, ids: Sequence[int], cap: int = MAX_EVENTS_PER_IMPORT) -> tuple[int, int]:
        """Emit ``finance.transaction`` for the newest ``cap`` of ``ids``: (emitted, held back)."""
        rows = [row for row in (self.ledger.transaction(txn_id) for txn_id in ids) if row is not None]
        rows.sort(key=lambda row: (row.posted, row.id), reverse=True)
        emitted = 0
        for row in rows[:cap]:
            event_id = self.ctx.events.emit(
                NAME, "finance.transaction", row.event_payload(), dedupe_key=f"finance:transaction:{row.id}"
            )
            if event_id is not None:
                emitted += 1
        return emitted, max(0, len(rows) - cap)

    def check_budgets(self, today: date) -> list[dict[str, Any]]:
        """Emit ``finance.budget_exceeded`` once per category per month for budgets over their limit."""
        if not self.settings.budgets:
            return []
        month = f"{today:%Y-%m}"
        start, end = month_bounds(month)
        totals = self.ledger.category_totals(start, end, self.settings.currency)
        fired: list[dict[str, Any]] = []
        for category, spent, limit in exceeded_budgets(totals, self.settings.budgets):
            if not self.ledger.record_budget_alert(category, month, spent, limit):
                continue
            payload = {
                "category": category,
                "month": month,
                "spent": cents_to_number(spent),
                "limit": cents_to_number(limit),
                "currency": self.settings.currency,
            }
            self.ctx.events.emit(
                NAME, "finance.budget_exceeded", payload, dedupe_key=f"finance:budget:{month}:{category}"
            )
            fired.append(payload)
        return fired

    # ---- reports

    def accounts(self) -> dict[str, Any]:
        return {
            "accounts": [row.to_dict() for row in self.ledger.accounts()],
            "note": "Balances come from the newest imported statement, not from a live bank connection.",
        }

    def transactions(self, query: TransactionQuery) -> dict[str, Any]:
        rows, more = self.ledger.transactions(query)
        return {"transactions": [row.to_dict() for row in rows], "count": len(rows), "more": more}

    def summary(self, month: str) -> dict[str, Any]:
        try:
            start, end = month_bounds(month)
        except ValueError as exc:
            raise ToolError("month must be a real month such as 2026-11") from exc
        totals = self.ledger.category_totals(start, end, self.settings.currency)
        return build_summary(
            month, self.settings.currency, totals, self.settings.budgets, self.settings.transfer_categories
        )

    def recurring(self, *, today: date, include_lapsed: bool = False) -> dict[str, Any]:
        charges = self.ledger.charges(today - timedelta(days=RECURRING_LOOKBACK_DAYS))
        found = detect_recurring(charges, today=today)
        return {
            "as_of": today.isoformat(),
            "recurring": [item.to_dict() for item in found if include_lapsed or item.active],
        }

    def recategorize(self) -> int:
        rules = self.settings.categories
        return self.ledger.recategorize(lambda *texts: categorize(rules, *texts))


def doctor_problems(ctx: AppContext) -> list[str]:
    """Configuration and inbox problems; never includes account numbers or file contents."""
    settings = load_settings(ctx.connector_config(NAME))
    problems = list(settings.problems)
    if settings.import_dir is not None:
        problems.extend(inbox_problems(settings.import_dir, strict=True))
    if settings.alert_rules and "mail" not in ctx.config.enabled_connectors():
        problems.append("alert_rules need the mail connector, which is not enabled")
    if settings.import_dir is None and not settings.alert_rules:
        problems.append("set import_dir or alert_rules; otherwise nothing reaches the ledger")
    return problems
