"""Parse and check the ``[connectors.finance]`` table.

Parsing is lenient: a broken entry (a regular expression that does not compile, a CSV profile without
an amount column) is dropped and described in :attr:`Settings.problems`, so ``switchboard doctor`` can
list every problem at once. ``register`` logs the same problems as warnings instead of refusing to start.
"""

from __future__ import annotations

import codecs
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from switchboard.connectors.finance.money import cents_from_number

DEFAULT_CURRENCY = "USD"
UNCATEGORIZED = "uncategorized"
DEFAULT_ALERT_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$"

_NAME = re.compile(NAME_PATTERN)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_LAST4 = re.compile(r"^\d{4}$")
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")
# A probe date whose day (31) cannot be mistaken for its month (01).
_PROBE = date(2026, 1, 31)
# Python 3.13 deprecates parsing a day of the month without a year, because it fails on February 29.
# Formats like that are probed with this leap year prefixed, which is not the probe's year, so the
# missing year is still reported.
_STAND_IN_YEAR = 2000
# A strptime directive: flags, a width, and an optional O or E modifier before the conversion character.
_DIRECTIVE = re.compile(r"%[-_0^#]*[0-9]*([OE]?.)", re.DOTALL)
_DAY_OF_MONTH = frozenset({"d", "e"})
_YEAR = frozenset({"Y", "y", "G"})
_COLUMN_KEYS = (
    "date_column",
    "description_column",
    "amount_column",
    "debit_column",
    "credit_column",
    "memo_column",
    "id_column",
)
_PROFILE_KEYS = frozenset(
    {
        "account",
        "files",
        "delimiter",
        "encoding",
        "skip_rows",
        "date_column",
        "date_format",
        "amount_column",
        "debit_column",
        "credit_column",
        "description_column",
        "memo_column",
        "id_column",
        "invert_sign",
        "decimal_mark",
        "currency",
    }
)


@dataclass(frozen=True)
class AccountConfig:
    """``[connectors.finance.accounts.<name>]``: a friendly name for an account."""

    name: str
    last4: str = ""
    kind: str = ""
    csv_profile: str = ""


@dataclass(frozen=True)
class CsvProfile:
    """``[connectors.finance.csv_profiles.<name>]``: how one bank lays out its CSV export."""

    name: str
    date_column: str
    description_column: str
    date_formats: tuple[str, ...] = ("%Y-%m-%d",)
    amount_column: str = ""
    debit_column: str = ""
    credit_column: str = ""
    memo_column: str = ""
    id_column: str = ""
    account: str = ""
    files: str = ""
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    skip_rows: int = 0
    invert_sign: bool = False
    decimal_mark: str = "."
    currency: str = ""


@dataclass(frozen=True)
class AlertRule:
    """``[[connectors.finance.alert_rules]]``: how to read one kind of bank alert email."""

    name: str
    senders: tuple[str, ...]
    body: re.Pattern[str]
    subject: re.Pattern[str] | None = None
    account: str = ""
    direction: str = "out"
    date_formats: tuple[str, ...] = DEFAULT_ALERT_DATE_FORMATS
    currency: str = ""


@dataclass(frozen=True)
class CategoryRule:
    """One ``[connectors.finance.categories]`` entry: a category and its case-insensitive patterns."""

    name: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Settings:
    """The parsed table. ``zone`` dates email headers and "today"; None means this computer's zone."""

    currency: str = DEFAULT_CURRENCY
    import_dir: Path | None = None
    zone: tzinfo | None = None
    accounts: Mapping[str, AccountConfig] = field(default_factory=dict)
    csv_profiles: Mapping[str, CsvProfile] = field(default_factory=dict)
    alert_rules: tuple[AlertRule, ...] = ()
    categories: tuple[CategoryRule, ...] = ()
    budgets: Mapping[str, int] = field(default_factory=dict)
    transfer_categories: frozenset[str] = frozenset({"transfer"})
    problems: tuple[str, ...] = ()

    def account_for_last4(self, last4: str) -> AccountConfig | None:
        """The configured account ending in ``last4``, when exactly one does."""
        matches = [account for account in self.accounts.values() if last4 and account.last4 == last4]
        return matches[0] if len(matches) == 1 else None

    def account_for_profile(self, profile: CsvProfile) -> str:
        """Ledger account for a CSV profile: its ``account``, an account naming it, or the profile name."""
        if profile.account:
            return profile.account
        for account in self.accounts.values():
            if account.csv_profile == profile.name:
                return account.name
        return profile.name


def load_settings(raw: Mapping[str, Any]) -> Settings:
    """Build :class:`Settings` from the ``[connectors.finance]`` table, collecting problems."""
    problems: list[str] = []
    settings = Settings(
        currency=_currency(raw.get("currency"), "currency", problems) or DEFAULT_CURRENCY,
        import_dir=_import_dir(raw.get("import_dir"), problems),
        zone=_zone(raw.get("timezone"), problems),
        accounts=_accounts(raw.get("accounts", {}), problems),
        csv_profiles=_csv_profiles(raw.get("csv_profiles", {}), problems),
        alert_rules=_alert_rules(raw.get("alert_rules", []), problems),
        categories=_categories(raw.get("categories", {}), problems),
        budgets=_budgets(raw.get("budgets", {}), problems),
        transfer_categories=_transfer_categories(raw.get("transfer_categories"), problems),
    )
    problems.extend(_cross_checks(settings))
    return replace(settings, problems=tuple(problems))


def date_format_problem(fmt: str) -> str | None:
    """Explain why a strptime format cannot read full dates, or return None when it can."""
    directives = {match[1] for match in _DIRECTIVE.finditer(fmt)}
    day_without_year = not directives.isdisjoint(_DAY_OF_MONTH) and directives.isdisjoint(_YEAR)
    try:
        text = _PROBE.strftime(fmt)
        if day_without_year:
            parsed = datetime.strptime(f"{_STAND_IN_YEAR} {text}", f"%Y {fmt}").date()
        else:
            parsed = datetime.strptime(text, fmt).date()
    except (ValueError, re.error):  # re.error: a repeated directive, such as "%d %d"
        return f"date_format {fmt!r} is not a valid strptime format"
    if parsed != _PROBE:
        return f"date_format {fmt!r} must include a year, a month, and a day"
    return None


def parse_timezone(name: str) -> tzinfo:
    """``UTC``, a fixed offset such as ``-08:00``, or a time zone name such as ``America/Los_Angeles``.

    Raises ValueError (or ZoneInfoNotFoundError, a KeyError) for anything else. UTC and fixed offsets
    work without the system's time zone database.
    """
    if name.upper() == "UTC":
        return UTC
    offset = _OFFSET.match(name)
    if offset is not None:
        sign, hours, minutes = offset.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        return timezone(-delta if sign == "-" else delta)
    return ZoneInfo(name)


def _zone(value: Any, problems: list[str]) -> tzinfo | None:
    if value is None:
        return None
    message = 'timezone must be "UTC", an offset such as "-08:00", or a name such as "America/Los_Angeles"'
    if not isinstance(value, str) or not value.strip():
        problems.append(message)
        return None
    try:
        return parse_timezone(value.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        problems.append(message)
        return None


def _table(value: Any, label: str, problems: list[str]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    problems.append(f"{label} must be a table")
    return {}


def _text(table: Mapping[str, Any], key: str, label: str, problems: list[str], default: str = "") -> str:
    value = table.get(key, default)
    if isinstance(value, str):
        return value
    problems.append(f"{label}: {key} must be a string")
    return default


def _strings(value: Any) -> list[str] | None:
    """A string or a non-empty list of non-empty strings, as a list; None for anything else."""
    items = [value] if isinstance(value, str) else value
    if isinstance(items, list) and items and all(isinstance(item, str) and item for item in items):
        return list(items)
    return None


def _regex(value: Any, label: str, problems: list[str], flags: int = 0) -> re.Pattern[str] | None:
    if not isinstance(value, str) or not value:
        problems.append(f"{label} must be a non-empty regular expression")
        return None
    try:
        return re.compile(value, flags)
    except re.error as exc:
        problems.append(f"{label} does not compile: {exc}")
        return None


def _currency(value: Any, label: str, problems: list[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and _CURRENCY.match(value):
        return value
    problems.append(f"{label} must be a three-letter currency code such as USD")
    return ""


def _formats(value: Any, label: str, problems: list[str], default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = _strings(value)
    if items is None:
        problems.append(f"{label}: date_format must be a strptime format or a list of them")
        return default
    good: list[str] = []
    for fmt in items:
        problem = date_format_problem(fmt)
        if problem is None:
            good.append(fmt)
        else:
            problems.append(f"{label}: {problem}")
    return tuple(good) or default


def _import_dir(value: Any, problems: list[str]) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append("import_dir must be a directory path")
        return None
    return Path(os.path.expanduser(value))


def _accounts(value: Any, problems: list[str]) -> dict[str, AccountConfig]:
    accounts: dict[str, AccountConfig] = {}
    for name, entry in _table(value, "accounts", problems).items():
        label = f"accounts.{name}"
        if not isinstance(entry, Mapping):
            problems.append(f"{label} must be a table")
            continue
        if not _NAME.match(name):
            problems.append(f"{label}: names use letters, digits, spaces, '.', '_', or '-' (64 at most)")
            continue
        last4 = entry.get("last4", "")
        if not isinstance(last4, str) or (last4 and not _LAST4.match(last4)):
            problems.append(f'{label}: last4 must be four digits in quotes, for example last4 = "1234"')
            last4 = ""
        accounts[name] = AccountConfig(
            name=name,
            last4=last4,
            kind=_text(entry, "kind", label, problems),
            csv_profile=_text(entry, "csv_profile", label, problems),
        )
    return accounts


def _csv_profiles(value: Any, problems: list[str]) -> dict[str, CsvProfile]:
    profiles: dict[str, CsvProfile] = {}
    for name, entry in _table(value, "csv_profiles", problems).items():
        label = f"csv_profiles.{name}"
        if not isinstance(entry, Mapping):
            problems.append(f"{label} must be a table")
            continue
        profile = _csv_profile(name, entry, label, problems)
        unknown = sorted(set(entry) - _PROFILE_KEYS)
        if unknown:
            problems.append(f"{label}: unknown key(s) {', '.join(unknown)}")
        if profile is not None:
            profiles[name] = profile
    return profiles


def _csv_profile(name: str, entry: Mapping[str, Any], label: str, problems: list[str]) -> CsvProfile | None:
    before = len(problems)
    columns = {key: _text(entry, key, label, problems) for key in _COLUMN_KEYS}
    _check_columns(columns, label, problems)
    files = _text(entry, "files", label, problems)
    delimiter = _text(entry, "delimiter", label, problems, ",")
    if len(delimiter) != 1:
        problems.append(f'{label}: delimiter must be one character (use "\\t" for tabs)')
    encoding = _text(entry, "encoding", label, problems, "utf-8-sig")
    if not _known_encoding(encoding):
        problems.append(f"{label}: unknown encoding {encoding!r}")
    skip_rows = entry.get("skip_rows", 0)
    if not isinstance(skip_rows, int) or isinstance(skip_rows, bool) or not 0 <= skip_rows <= 100:
        problems.append(f"{label}: skip_rows must be a whole number from 0 to 100")
        skip_rows = 0
    invert_sign = entry.get("invert_sign", False)
    if not isinstance(invert_sign, bool):
        problems.append(f"{label}: invert_sign must be true or false")
    decimal_mark = _text(entry, "decimal_mark", label, problems, ".")
    if decimal_mark not in {".", ","}:
        problems.append(f'{label}: decimal_mark must be "." or ","')
    account = _text(entry, "account", label, problems)
    if account and not _NAME.match(account):
        problems.append(f"{label}: account names use letters, digits, spaces, '.', '_', or '-'")
    currency = _currency(entry.get("currency"), f"{label}: currency", problems)
    formats = _formats(entry.get("date_format"), label, problems, ("%Y-%m-%d",))
    if len(problems) > before:
        return None
    return CsvProfile(
        name=name,
        date_column=columns["date_column"],
        description_column=columns["description_column"],
        date_formats=formats,
        amount_column=columns["amount_column"],
        debit_column=columns["debit_column"],
        credit_column=columns["credit_column"],
        memo_column=columns["memo_column"],
        id_column=columns["id_column"],
        account=account,
        files=files,
        delimiter=delimiter,
        encoding=encoding,
        skip_rows=skip_rows,
        invert_sign=bool(invert_sign),
        decimal_mark=decimal_mark,
        currency=currency,
    )


def _check_columns(columns: Mapping[str, str], label: str, problems: list[str]) -> None:
    for required in ("date_column", "description_column"):
        if not columns[required]:
            problems.append(f"{label}: {required} is required")
    split = bool(columns["debit_column"] or columns["credit_column"])
    if bool(columns["amount_column"]) == split:
        problems.append(f"{label}: set amount_column, or debit_column and credit_column, but not both")
    elif split and not (columns["debit_column"] and columns["credit_column"]):
        problems.append(f"{label}: debit_column and credit_column must be set together")


def _known_encoding(name: str) -> bool:
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True


def _alert_rules(value: Any, problems: list[str]) -> tuple[AlertRule, ...]:
    if not isinstance(value, list):
        problems.append("alert_rules must be an array of tables ([[connectors.finance.alert_rules]])")
        return ()
    rules: list[AlertRule] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            problems.append(f"alert_rules[{index}] must be a table")
            continue
        rule = _alert_rule(index, entry, problems)
        if rule is not None:
            rules.append(rule)
    return tuple(rules)


def _alert_rule(index: int, entry: Mapping[str, Any], problems: list[str]) -> AlertRule | None:
    raw_name = entry.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else f"alert_rules[{index}]"
    label = f"alert rule {name!r}"
    before = len(problems)
    senders = _strings(entry.get("from"))
    if senders is None:
        problems.append(f"{label}: 'from' must name a sender address or glob, or a list of them")
    body = _regex(entry.get("body"), f"{label}: body", problems)
    if body is not None and "amount" not in body.groupindex:
        problems.append(f"{label}: body needs a named group (?P<amount>...)")
    subject = None
    if entry.get("subject") is not None:
        subject = _regex(entry.get("subject"), f"{label}: subject", problems)
    direction = entry.get("direction", "out")
    if direction not in {"out", "in"}:
        problems.append(f'{label}: direction must be "out" (money spent) or "in" (money received)')
    account = _text(entry, "account", label, problems)
    currency = _currency(entry.get("currency"), f"{label}: currency", problems)
    formats = _formats(entry.get("date_format"), label, problems, DEFAULT_ALERT_DATE_FORMATS)
    if len(problems) > before or body is None or senders is None:
        return None
    return AlertRule(
        name=name,
        senders=tuple(senders),
        body=body,
        subject=subject,
        account=account,
        direction=str(direction),
        date_formats=formats,
        currency=currency,
    )


def _categories(value: Any, problems: list[str]) -> tuple[CategoryRule, ...]:
    rules: list[CategoryRule] = []
    for name, patterns in _table(value, "categories", problems).items():
        items = _strings(patterns)
        if items is None:
            problems.append(f"categories.{name} must be a list of regular expressions")
            continue
        compiled = [
            _regex(item, f"categories.{name}[{position}]", problems, re.IGNORECASE)
            for position, item in enumerate(items)
        ]
        valid = tuple(pattern for pattern in compiled if pattern is not None)
        if valid:
            rules.append(CategoryRule(str(name), valid))
    return tuple(rules)


def _budgets(value: Any, problems: list[str]) -> dict[str, int]:
    budgets: dict[str, int] = {}
    for category, limit in _table(value, "budgets", problems).items():
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not math.isfinite(limit)
            or limit <= 0
        ):
            problems.append(f"budgets.{category} must be a positive monthly amount")
            continue
        budgets[str(category)] = cents_from_number(limit)
    return budgets


def _transfer_categories(value: Any, problems: list[str]) -> frozenset[str]:
    if value is None:
        return frozenset({"transfer"})
    items = _strings(value)
    if items is None:
        problems.append("transfer_categories must be a list of category names")
        return frozenset({"transfer"})
    return frozenset(items)


def _cross_checks(settings: Settings) -> list[str]:
    problems: list[str] = []
    owners: dict[str, str] = {}
    for account in settings.accounts.values():
        if account.last4 and account.last4 in owners:
            problems.append(
                f"accounts.{account.name} and accounts.{owners[account.last4]} both end in "
                f"{account.last4}; imports cannot tell them apart"
            )
        elif account.last4:
            owners[account.last4] = account.name
        if account.csv_profile and account.csv_profile not in settings.csv_profiles:
            problems.append(f"accounts.{account.name}: csv_profile {account.csv_profile!r} is not defined")
    known = {rule.name for rule in settings.categories} | {UNCATEGORIZED}
    problems.extend(
        f"budgets.{category} has no [connectors.finance.categories] entry, so nothing will count toward it"
        for category in settings.budgets
        if category not in known
    )
    return problems
