"""Pure analysis of ledger rows: monthly summaries, recurring charges, and budgets.

Every function here takes its inputs, including ``today``, as arguments, so results are reproducible.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from switchboard.connectors.finance.money import cents_to_number

MIN_OCCURRENCES = 3


@dataclass(frozen=True)
class Cadence:
    name: str
    days: int
    tolerance: int


MONTHLY = Cadence("monthly", days=30, tolerance=5)
WEEKLY = Cadence("weekly", days=7, tolerance=2)


@dataclass(frozen=True)
class Charge:
    """One ledger transaction, as recurring detection sees it."""

    id: int
    account: str
    merchant: str
    category: str
    posted: date
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class Recurring:
    merchant: str
    account: str
    category: str
    cadence: str
    typical_cents: int
    occurrences: int
    first: date
    last: date
    next_expected: date
    active: bool
    currency: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "account": self.account,
            "category": self.category,
            "cadence": self.cadence,
            "typical_amount": cents_to_number(self.typical_cents),
            "currency": self.currency,
            "occurrences": self.occurrences,
            "first_date": self.first.isoformat(),
            "last_date": self.last.isoformat(),
            "next_expected": self.next_expected.isoformat(),
            "status": "active" if self.active else "lapsed",
        }


@dataclass(frozen=True)
class CategoryTotal:
    """Money in and out of one category in one month; ``outflow_cents`` is a positive number."""

    category: str
    inflow_cents: int
    outflow_cents: int
    count: int
    pending: int


def add_months(day: date, months: int) -> date:
    """Same day of the month ``months`` later, clamped to the month's length (Jan 31 + 1 = Feb 28)."""
    index = day.year * 12 + day.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def month_bounds(month: str) -> tuple[date, date]:
    """First day of ``YYYY-MM`` and first day of the next month; ValueError for a malformed month."""
    start = date.fromisoformat(f"{month}-01")
    return start, add_months(start, 1)


def detect_recurring(charges: Iterable[Charge], *, today: date) -> list[Recurring]:
    """Subscriptions and other repeating charges, soonest expected first.

    A merchant counts when at least three charges, ending with its latest, repeat monthly (every
    30 +/- 5 days) or weekly (every 7 +/- 2 days) for amounts within 10% of the latest charge. Only money
    out is considered. ``active`` is false when the next charge is overdue by more than the tolerance.
    """
    by_merchant: dict[str, list[Charge]] = defaultdict(list)
    for charge in charges:
        if charge.amount_cents < 0 and charge.merchant:
            by_merchant[charge.merchant].append(charge)
    found: list[Recurring] = []
    for merchant in sorted(by_merchant):
        items = sorted(by_merchant[merchant], key=lambda charge: (charge.posted, charge.id))
        for cadence in (MONTHLY, WEEKLY):
            run = recent_run(items, cadence)
            if len(run) >= MIN_OCCURRENCES:
                found.append(_recurring(run, cadence, today))
                break
    return sorted(found, key=lambda item: (item.next_expected, item.merchant))


def recent_run(items: Sequence[Charge], cadence: Cadence) -> list[Charge]:
    """The charges, ending with the latest, that repeat on ``cadence``; oldest first.

    Walking back from the newest charge, a charge whose amount differs by more than 10% is a different
    purchase from the same merchant and is skipped. Any other charge must come one cycle (plus or minus
    the tolerance) before the previous one, or the run ends: a gap means a missed cycle, and a short
    interval means frequent purchases (a daily coffee), not a subscription.
    """
    if not items:
        return []
    latest = items[-1]
    run = [latest]
    for earlier in reversed(items[:-1]):
        if not within_ten_percent(earlier.amount_cents, latest.amount_cents):
            continue
        gap = (run[-1].posted - earlier.posted).days
        if abs(gap - cadence.days) > cadence.tolerance:
            break
        run.append(earlier)
    run.reverse()
    return run


def within_ten_percent(amount: int, reference: int) -> bool:
    """True when ``amount`` is within 10% of ``reference``, in integer arithmetic."""
    return abs(amount - reference) * 10 <= abs(reference)


def _recurring(run: Sequence[Charge], cadence: Cadence, today: date) -> Recurring:
    amounts = sorted(charge.amount_cents for charge in run)
    last = run[-1]
    step = timedelta(days=cadence.days)
    expected = add_months(last.posted, 1) if cadence is MONTHLY else last.posted + step
    return Recurring(
        merchant=last.merchant,
        account=last.account,
        category=last.category,
        cadence=cadence.name,
        typical_cents=amounts[len(amounts) // 2],
        occurrences=len(run),
        first=run[0].posted,
        last=last.posted,
        next_expected=expected,
        active=today <= expected + timedelta(days=cadence.tolerance),
        currency=last.currency,
    )


def budget_usage(total: CategoryTotal) -> int:
    """Spending that counts against a budget: money out minus refunds, never below zero."""
    return max(0, total.outflow_cents - total.inflow_cents)


def exceeded_budgets(
    totals: Sequence[CategoryTotal], budgets: Mapping[str, int]
) -> list[tuple[str, int, int]]:
    """``(category, spent_cents, limit_cents)`` for every budget whose usage is above its limit."""
    usage = {total.category: budget_usage(total) for total in totals}
    return [
        (category, usage[category], limit)
        for category, limit in sorted(budgets.items())
        if usage.get(category, 0) > limit
    ]


def build_summary(
    month: str,
    currency: str,
    totals: Sequence[CategoryTotal],
    budgets: Mapping[str, int],
    transfer_categories: Collection[str],
) -> dict[str, Any]:
    """Income, spending, and per-category totals for one month.

    Categories listed in ``transfer_categories`` (paying a card from checking, moving money to savings)
    are reported but left out of income and spending, so moving money between your own accounts does
    not count twice. Budgets without any activity this month still appear, fully remaining.
    """
    income = spending = transfers_in = transfers_out = 0
    categories: list[dict[str, Any]] = []
    for total in totals:
        entry = _category_entry(total, budgets.get(total.category))
        if total.category in transfer_categories:
            entry["transfer"] = True
            transfers_in += total.inflow_cents
            transfers_out += total.outflow_cents
        else:
            income += total.inflow_cents
            spending += total.outflow_cents
        categories.append(entry)
    seen = {total.category for total in totals}
    categories.extend(
        _category_entry(CategoryTotal(category, 0, 0, 0, 0), budgets[category])
        for category in sorted(set(budgets) - seen)
    )
    return {
        "month": month,
        "currency": currency,
        "income": cents_to_number(income),
        "spending": cents_to_number(spending),
        "net": cents_to_number(income - spending),
        "transfers": {"in": cents_to_number(transfers_in), "out": cents_to_number(transfers_out)},
        "categories": categories,
    }


def _category_entry(total: CategoryTotal, limit: int | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "category": total.category,
        "income": cents_to_number(total.inflow_cents),
        "spending": cents_to_number(total.outflow_cents),
        "net": cents_to_number(total.inflow_cents - total.outflow_cents),
        "count": total.count,
        "pending": total.pending,
    }
    if limit is not None:
        entry["budget"] = cents_to_number(limit)
        entry["remaining"] = cents_to_number(limit - budget_usage(total))
    return entry
