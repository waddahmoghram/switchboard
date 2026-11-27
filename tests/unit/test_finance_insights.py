"""Recurring charges, monthly summaries, budgets, and month arithmetic."""

from __future__ import annotations

import re
from datetime import date

import pytest

from switchboard.connectors.finance.insights import (
    CategoryTotal,
    Charge,
    add_months,
    build_summary,
    detect_recurring,
    exceeded_budgets,
    month_bounds,
)

TODAY = date(2026, 11, 10)


def charges(merchant: str, *dated: tuple[date, int]) -> list[Charge]:
    return [
        Charge(index, "everyday", merchant, "subscriptions", posted, cents, "USD")
        for index, (posted, cents) in enumerate(dated, start=1)
    ]


def test_monthly_subscription_with_small_price_variation() -> None:
    history = charges(
        "EXAMPLESTREAM.COM",
        (date(2026, 8, 4), -1549),
        (date(2026, 9, 3), -1549),
        (date(2026, 10, 4), -1599),
        (date(2026, 11, 4), -1549),
    )

    [found] = detect_recurring(history, today=TODAY)

    assert found.to_dict() == {
        "merchant": "EXAMPLESTREAM.COM",
        "account": "everyday",
        "category": "subscriptions",
        "cadence": "monthly",
        "typical_amount": -15.49,
        "currency": "USD",
        "occurrences": 4,
        "first_date": "2026-08-04",
        "last_date": "2026-11-04",
        "next_expected": "2026-12-04",
        "status": "active",
    }


def test_weekly_charges() -> None:
    history = charges(
        "EXAMPLE GYM",
        (date(2026, 10, 13), -1200),
        (date(2026, 10, 20), -1200),
        (date(2026, 10, 28), -1200),
        (date(2026, 11, 4), -1200),
    )

    [found] = detect_recurring(history, today=TODAY)

    assert (found.cadence, found.occurrences, found.next_expected) == ("weekly", 4, date(2026, 11, 11))


def test_other_purchases_from_the_same_merchant_are_skipped() -> None:
    history = charges(
        "EXAMPLE SHOP",
        (date(2026, 8, 15), -1299),
        (date(2026, 9, 15), -1299),
        (date(2026, 9, 20), -4500),
        (date(2026, 10, 2), -4500),
        (date(2026, 10, 15), -1299),
    )

    [found] = detect_recurring(history, today=TODAY)

    assert (found.cadence, found.occurrences, found.typical_cents) == ("monthly", 3, -1299)


@pytest.mark.parametrize(
    "dated",
    [
        # Only two charges.
        ((date(2026, 9, 4), -1549), (date(2026, 10, 4), -1549)),
        # Irregular gaps.
        ((date(2026, 8, 1), -1549), (date(2026, 8, 20), -1549), (date(2026, 10, 15), -1549)),
        # Amounts that differ by more than 10%.
        (
            (date(2026, 8, 4), -1000),
            (date(2026, 9, 4), -1549),
            (date(2026, 10, 4), -2000),
            (date(2026, 11, 4), -1549),
        ),
        # A daily purchase is not a weekly subscription.
        tuple((date(2026, 11, day), -350) for day in range(1, 10)),
    ],
)
def test_non_recurring_patterns(dated: tuple[tuple[date, int], ...]) -> None:
    assert detect_recurring(charges("EXAMPLE MERCHANT", *dated), today=TODAY) == []


def test_money_in_is_never_a_subscription() -> None:
    history = charges(
        "EXAMPLE EMPLOYER",
        (date(2026, 9, 1), 250000),
        (date(2026, 10, 1), 250000),
        (date(2026, 11, 1), 250000),
    )

    assert detect_recurring(history, today=TODAY) == []


def test_lapsed_subscription() -> None:
    history = charges(
        "EXAMPLE NEWS",
        (date(2026, 6, 4), -999),
        (date(2026, 7, 4), -999),
        (date(2026, 8, 4), -999),
    )

    [found] = detect_recurring(history, today=TODAY)

    assert (found.next_expected, found.active, found.to_dict()["status"]) == (
        date(2026, 9, 4),
        False,
        "lapsed",
    )


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
        (date(2026, 3, 31), -1, date(2026, 2, 28)),
    ],
)
def test_add_months_clamps_to_the_month_length(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected


def test_month_bounds() -> None:
    assert month_bounds("2026-11") == (date(2026, 11, 1), date(2026, 12, 1))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))
    with pytest.raises(ValueError, match=re.escape("month must be in 1..12")):
        month_bounds("2026-13")


TOTALS = [
    CategoryTotal("groceries", 0, 30000, 5, 0),
    CategoryTotal("income", 250000, 0, 1, 0),
    CategoryTotal("transfer", 50000, 50000, 2, 0),
    CategoryTotal("dining", 500, 4500, 3, 1),
]
BUDGETS = {"groceries": 25000, "dining": 10000, "travel": 50000}


def test_summary_separates_income_spending_and_transfers() -> None:
    summary = build_summary("2026-11", "USD", TOTALS, BUDGETS, {"transfer"})

    assert (summary["income"], summary["spending"], summary["net"]) == (2505.0, 345.0, 2160.0)
    assert summary["transfers"] == {"in": 500.0, "out": 500.0}
    by_name = {entry["category"]: entry for entry in summary["categories"]}
    assert (by_name["groceries"]["budget"], by_name["groceries"]["remaining"]) == (250.0, -50.0)
    assert (by_name["dining"]["remaining"], by_name["dining"]["pending"]) == (60.0, 1)
    assert by_name["transfer"]["transfer"] is True
    assert by_name["travel"] == {
        "category": "travel",
        "income": 0.0,
        "spending": 0.0,
        "net": 0.0,
        "count": 0,
        "pending": 0,
        "budget": 500.0,
        "remaining": 500.0,
    }


def test_exceeded_budgets_count_refunds_against_spending() -> None:
    assert exceeded_budgets(TOTALS, BUDGETS) == [("groceries", 30000, 25000)]
    assert exceeded_budgets([CategoryTotal("dining", 2000, 11000, 2, 0)], {"dining": 10000}) == []
