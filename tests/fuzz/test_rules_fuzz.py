"""Seeded fuzzing of rule loading and rule matching.

``load_rules`` receives generated configuration with wrong types, unknown keys, empty names, malformed
conditions, and random regular expressions, and must return rules or raise ``ConfigError``. Rules built from
well-formed parts are then matched against generated events and their action arguments rendered; matching
must return a boolean and rendering must never raise. Match paths stay ASCII: a path segment such as a
superscript digit passes ``str.isdigit`` but not ``int``, which is a separate review note, not a fuzz target.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from switchboard.errors import ConfigError
from switchboard.events import Event
from switchboard.rules import Rule, event_context, load_rules, matches, render
from tests.fuzz.mutation import SAFE_PATTERNS, generator, iterations, json_value, pattern, text

pytestmark = pytest.mark.fuzz

JUNK: tuple[Any, ...] = (None, True, 0, 2.5, "", "text", [], {})
PATHS = (
    "payload.amount",
    "payload.currency",
    "payload.items.0",
    "payload.items.1.name",
    "event.type",
    "event.id",
)
BAD_PATHS: tuple[Any, ...] = ("", ".amount", "payload..amount", "payload.", 7)
OPERATORS = ("equals", "contains", "regex", "in", "exists", "gte", "lte")
BAD_IN: tuple[Any, ...] = ([], "abc", [1])
EVENT_TYPES = ("finance.transaction", "home.state_changed", "mail.received", "x")
ON_PATTERNS = ("finance.*", "*", "home.state_changed", "mail.*", "[", "*.*.*")


def condition(rng: random.Random, *, well_formed: bool) -> Any:
    if rng.random() < 0.4:
        # A plain value means "equals"; a table is read as operators, so well-formed values avoid tables.
        if well_formed:
            return rng.choice((None, True, 0, -100, 2.5, "USD", "", [1, "a"]))
        return json_value(rng, 2)
    table: dict[Any, Any] = {}
    for _ in range(rng.randint(1 if well_formed else 0, 3)):
        operator = rng.choice(OPERATORS)
        if operator == "regex":
            table[operator] = rng.choice(SAFE_PATTERNS) if well_formed else pattern(rng)
        elif operator in ("gte", "lte"):
            table[operator] = (
                rng.choice((0, -100, 2.5))
                if well_formed
                else rng.choice((0, True, "1", float("nan"), 10**400))
            )
        elif operator == "exists":
            table[operator] = rng.random() < 0.5 if well_formed else rng.choice((True, "yes", 1))
        elif operator == "in":
            table[operator] = [json_value(rng, 2) for _ in range(3)] if well_formed else rng.choice(BAD_IN)
        elif operator == "contains":
            table[operator] = text(rng, 4) if well_formed else rng.choice(("a", 3, None))
        else:
            table[operator] = json_value(rng, 2)
    if not well_formed and rng.random() < 0.2:
        table[rng.choice(("matches", "not", 3))] = 1
    return table


def action(rng: random.Random, *, well_formed: bool) -> Any:
    if not well_formed and rng.random() < 0.1:
        return rng.choice(JUNK)
    item: dict[Any, Any] = {
        "tool": "notify_self" if well_formed else rng.choice(("notify_self", "", "  ", 5, None))
    }
    if rng.random() < 0.7:
        item["args"] = {
            "title": "{payload.merchant}",
            "message": "{{literal}} {payload.amount} at {event.type}",
            "whole": "{payload.items}",
            "missing": "{payload.nothing.here}",
        }
        if not well_formed and rng.random() < 0.3:
            item["args"] = rng.choice(JUNK)
    if rng.random() < 0.3:
        item["stop_on_error"] = rng.random() < 0.5 if well_formed else rng.choice((True, "no", 0))
    if not well_formed and rng.random() < 0.1:
        item["retry"] = True
    return item


def raw_rule(rng: random.Random, *, well_formed: bool) -> Any:
    if not well_formed and rng.random() < 0.05:
        return rng.choice(JUNK)
    rule: dict[Any, Any] = {}
    if rng.random() < 0.9:
        rule["name"] = text(rng, 8) + "rule" if well_formed else rng.choice(("alert", "", "  ", 5, None))
    if well_formed or rng.random() < 0.95:
        rule["on"] = rng.choice(ON_PATTERNS) if well_formed else rng.choice((*ON_PATTERNS, "", 3, None))
    if rng.random() < 0.8:
        keys = PATHS if well_formed else (*PATHS, *BAD_PATHS)
        rule["match"] = {
            rng.choice(keys): condition(rng, well_formed=well_formed) for _ in range(rng.randint(0, 3))
        }
        if not well_formed and rng.random() < 0.1:
            rule["match"] = rng.choice(JUNK)
    if well_formed or rng.random() < 0.95:
        count = rng.randint(1, 3) if well_formed else rng.randint(0, 3)
        rule["do"] = [action(rng, well_formed=well_formed) for _ in range(count)]
        if not well_formed and rng.random() < 0.1:
            rule["do"] = rng.choice(JUNK)
    if not well_formed and rng.random() < 0.1:
        rule[rng.choice(("when", "then", 1))] = "unexpected"
    return rule


def event(rng: random.Random, number: int) -> Event:
    payload: dict[str, Any] = {
        "amount": rng.choice((-150, -20.5, 0, 99, "12.50", None, "NaN", True, 10**30)),
        "currency": rng.choice(("USD", "usd", "EUR", None, 3)),
        "merchant": text(rng, 10),
        "items": [json_value(rng, 2) for _ in range(rng.randint(0, 3))],
    }
    if rng.random() < 0.3:
        payload[text(rng, 5)] = json_value(rng, 1)
    return Event(number, "synthetic", rng.choice(EVENT_TYPES), payload, "2026-11-11T00:00:00+00:00")


def test_load_rules_returns_rules_or_raises_config_error() -> None:
    rng = generator("load_rules")
    loaded = rejected = 0
    for _ in range(iterations()):
        raw: Any = (
            rng.choice(JUNK)
            if rng.random() < 0.05
            else [raw_rule(rng, well_formed=rng.random() < 0.3) for _ in range(rng.randint(0, 3))]
        )
        try:
            rules = load_rules(raw)
        except ConfigError:
            rejected += 1
            continue
        loaded += 1
        assert all(isinstance(rule, Rule) for rule in rules)
    assert loaded > 0
    assert rejected > 0


def test_loaded_rules_match_and_render_without_raising() -> None:
    rng = generator("rule_matching")
    fired = 0
    for number in range(iterations()):
        rules = load_rules([raw_rule(rng, well_formed=True) for _ in range(rng.randint(1, 3))])
        incoming = event(rng, number)
        context = event_context(incoming)
        for rule in rules:
            result = matches(rule, incoming)
            assert isinstance(result, bool)
            assert matches(rule, incoming) is result
            if not result:
                continue
            fired += 1
            for item in rule.actions:
                rendered = render(dict(item.get("args", {})), context)
                assert isinstance(rendered, dict)
    assert fired > 0
