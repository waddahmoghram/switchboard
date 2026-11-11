"""Automation rules: "when this event happens, call these tools" — a local Zapier.

Rules live in the configuration as ``[[rules]]`` tables::

    [[rules]]
    name = "card alert to phone"
    on = "finance.transaction"            # event type, globs allowed
    [rules.match]
    "payload.amount" = { lte = -100 }     # expenses are negative; every condition must hold
    "payload.currency" = "USD"
    "payload.pending" = true
    [[rules.do]]
    tool = "notify_self"
    args = { title = "Card charge", message = "{payload.amount} at {payload.merchant}" }

Conditions: a plain value means *equals*; a table may combine ``equals``, ``contains``
(case-insensitive), ``regex``, ``in``, ``exists``, ``gte``, and ``lte``.

Templates substitute ``{dotted.path}`` from ``{"event": …, "payload": …}``. A string that is exactly
one placeholder keeps the value's type. ``{{`` and ``}}`` produce literal braces. Nothing is evaluated
as code, and every action goes through the policy gate with origin ``rule``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from switchboard.audit import AuditLog
from switchboard.errors import ConfigError
from switchboard.events import Event
from switchboard.policy import Origin
from switchboard.tools import Dispatcher
from switchboard.util import glob_any, json_equal

_PATH = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*"
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{(" + _PATH + r")\}")
_WHOLE = re.compile(r"\{(" + _PATH + r")\}")
_RULE_KEYS = frozenset({"name", "on", "match", "do"})
_ACTION_KEYS = frozenset({"tool", "args", "stop_on_error"})
_OPERATORS = frozenset({"equals", "contains", "regex", "in", "exists", "gte", "lte"})


@dataclass(frozen=True)
class Rule:
    name: str
    on: str
    match: Mapping[str, Any]
    actions: tuple[Mapping[str, Any], ...]


def load_rules(raw: Any) -> list[Rule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("[[rules]] must be an array of tables")
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        label = f"rules[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{label} must be a table")
        _check_keys(item, _RULE_KEYS, label)
        name = item.get("name", label)
        on = item.get("on")
        actions = item.get("do", [])
        match = item.get("match", {})
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{label}: 'name' must be a non-empty string")
        if not isinstance(on, str) or not on.strip():
            raise ConfigError(f"{label}: 'on' must name an event type")
        if not isinstance(match, Mapping):
            raise ConfigError(f"{label}: 'match' must be a table")
        for path, condition in match.items():
            if not isinstance(path, str) or any(not part for part in path.split(".")):
                raise ConfigError(f"{label}: match keys must be non-empty dotted paths")
            _check_condition(condition, f"{label}.match.{path}")
        if not isinstance(actions, list) or not actions:
            raise ConfigError(f"{label}: 'do' must list at least one action")
        for action in actions:
            if (
                not isinstance(action, Mapping)
                or not isinstance(action.get("tool"), str)
                or not action["tool"].strip()
            ):
                raise ConfigError(f"{label}: every action needs a 'tool' name")
            _check_keys(action, _ACTION_KEYS, label)
            if not isinstance(action.get("args", {}), Mapping):
                raise ConfigError(f"{label}: action 'args' must be a table")
            if "stop_on_error" in action and not isinstance(action["stop_on_error"], bool):
                raise ConfigError(f"{label}: stop_on_error must be a boolean")
        rules.append(Rule(name, on, match, tuple(actions)))
    return rules


def _check_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ConfigError(f"{label}: unknown setting(s): {', '.join(unknown)}")


def _check_condition(condition: Any, label: str) -> None:
    if not isinstance(condition, Mapping):
        return
    _check_keys(condition, _OPERATORS, label)
    if not condition:
        raise ConfigError(f"{label}: a condition table needs at least one operator")
    for key in ("gte", "lte"):
        if key in condition and (
            not isinstance(condition[key], (int, float)) or _number(condition[key]) is None
        ):
            raise ConfigError(f"{label}.{key}: expected a finite number")
    if "exists" in condition and not isinstance(condition["exists"], bool):
        raise ConfigError(f"{label}.exists: expected a boolean")
    if "in" in condition and not isinstance(condition["in"], list):
        raise ConfigError(f"{label}.in: expected a list")
    for key in ("contains", "regex"):
        if key in condition and not isinstance(condition[key], str):
            raise ConfigError(f"{label}.{key}: expected a string")
    if "regex" in condition:
        try:
            re.compile(condition["regex"])
        except re.error as exc:
            raise ConfigError(f"{label}.regex: invalid regular expression") from exc


def event_context(event: Event) -> dict[str, Any]:
    return {
        "event": {"id": event.id, "source": event.source, "type": event.type, "created_at": event.created_at},
        "payload": event.payload,
    }


def lookup(context: Mapping[str, Any], dotted: str) -> Any:
    node: Any = context
    for part in dotted.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def condition_holds(value: Any, condition: Any) -> bool:
    if not isinstance(condition, Mapping):
        return json_equal(value, condition)
    checks: list[bool] = []
    if "equals" in condition:
        checks.append(json_equal(value, condition["equals"]))
    if "contains" in condition:
        needle = str(condition["contains"]).lower()
        if isinstance(value, list):
            checks.append(any(needle == str(item).lower() for item in value))
        else:
            checks.append(value is not None and needle in str(value).lower())
    if "regex" in condition:
        checks.append(value is not None and re.search(str(condition["regex"]), str(value)) is not None)
    if "in" in condition:
        checks.append(any(json_equal(value, item) for item in condition["in"]))
    if "exists" in condition:
        checks.append((value is not None) == bool(condition["exists"]))
    number = _number(value)
    if "gte" in condition:
        checks.append(number is not None and number >= float(condition["gte"]))
    if "lte" in condition:
        checks.append(number is not None and number <= float(condition["lte"]))
    return bool(checks) and all(checks)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def matches(rule: Rule, event: Event) -> bool:
    if not glob_any(event.type, [rule.on]):
        return False
    context = event_context(event)
    return all(condition_holds(lookup(context, path), condition) for path, condition in rule.match.items())


def render(template: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(template, str):
        whole = _WHOLE.fullmatch(template)
        if whole:
            return lookup(context, whole.group(1))

        def substitute(match: re.Match[str]) -> str:
            piece = match.group(0)
            if piece == "{{":
                return "{"
            if piece == "}}":
                return "}"
            value = lookup(context, match.group(1))
            return "" if value is None else str(value)

        return _PLACEHOLDER.sub(substitute, template)
    if isinstance(template, Mapping):
        return {key: render(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [render(item, context) for item in template]
    return template


class RuleEngine:
    def __init__(self, rules: list[Rule], dispatcher: Dispatcher, audit: AuditLog) -> None:
        self.rules = rules
        self.dispatcher = dispatcher
        self.audit = audit

    def __call__(self, event: Event) -> None:
        for rule in self.rules:
            if not matches(rule, event):
                continue
            context = event_context(event)
            for action in rule.actions:
                args = render(dict(action.get("args", {})), context)
                outcome = self.dispatcher.call(str(action["tool"]), args, Origin.RULE)
                self.audit.append(
                    "rule.fired", rule=rule.name, event=event.id, tool=action["tool"], status=outcome.status
                )
                if outcome.is_error and action.get("stop_on_error", True):
                    break
