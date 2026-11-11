"""Automation rules: conditions, templating, validation, and the engine."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard.errors import ConfigError
from switchboard.events import Event
from switchboard.policy import Tier
from switchboard.rules import Rule, condition_holds, load_rules, matches, render
from switchboard.tools import Tool
from tests.conftest import AppFactory

EVENT = Event(
    7,
    "finance",
    "finance.transaction",
    {"amount": -120.5, "merchant": "Example Grocer", "pending": True, "tags": ["Food"]},
    "2026-11-11T00:00:00+00:00",
)


def test_conditions() -> None:
    assert condition_holds(5, 5)
    assert condition_holds("Example Grocer", {"contains": "grocer"})
    assert condition_holds(["Food"], {"contains": "food"})
    assert condition_holds("A-123", {"regex": r"^A-\d+$"})
    assert condition_holds("b", {"in": ["a", "b"]})
    assert condition_holds(None, {"exists": False})
    assert condition_holds(-120.5, {"lte": -100, "gte": -200})
    assert not condition_holds("12", {"gte": 20})
    assert not condition_holds(True, {"gte": 0})
    assert not condition_holds("anything", {})


def test_matching_and_rendering() -> None:
    rule = Rule(
        "big", "finance.*", {"payload.amount": {"lte": -100}, "payload.pending": True}, ({"tool": "x"},)
    )
    assert matches(rule, EVENT)
    context: dict[str, Any] = {"event": {"id": 7}, "payload": EVENT.payload}
    assert render("{payload.amount} at {payload.merchant}", context) == "-120.5 at Example Grocer"
    assert render("{payload.amount}", context) == -120.5
    assert render("{{literal}} {payload.missing}!", context) == "{literal} !"
    assert render({"list": ["{event.id}"]}, context) == {"list": [7]}
    assert render("{payload.tags.0}", context) == "Food"


def test_rules_that_do_not_match() -> None:
    other_type = Rule("r", "mail.*", {}, ({"tool": "x"},))
    assert not matches(other_type, EVENT)
    small = Rule("r", "finance.transaction", {"payload.amount": {"lte": -500}}, ({"tool": "x"},))
    assert not matches(small, EVENT)


def test_load_rules_validates_shape() -> None:
    rules = load_rules(
        [{"name": "n", "on": "mail.received", "do": [{"tool": "notify_self", "args": {"t": 1}}]}]
    )
    assert rules[0].on == "mail.received"
    assert load_rules(None) == []
    with pytest.raises(ConfigError):
        load_rules([{"on": "x", "do": []}])
    with pytest.raises(ConfigError):
        load_rules([{"do": [{"tool": "a"}]}])
    with pytest.raises(ConfigError):
        load_rules({"not": "a list"})
    with pytest.raises(ConfigError):
        load_rules([{"on": "x", "do": [{"args": {}}]}])


def test_rule_engine_calls_tools_through_the_policy_gate(make_app: AppFactory) -> None:
    app = make_app(
        {
            "rules": [
                {
                    "name": "alert",
                    "on": "demo.*",
                    "match": {"payload.level": {"gte": 5}},
                    "do": [
                        {"tool": "demo_note", "args": {"text": "level {payload.level}"}},
                        {"tool": "demo_act", "args": {}},
                    ],
                }
            ]
        }
    )
    notes: list[str] = []
    note_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }
    app.registry.add(
        Tool("demo_note", "Note.", Tier.DRAFT, note_schema, lambda args: notes.append(args["text"]))
    )
    app.registry.add(Tool("demo_act", "Act.", Tier.ACT, {"type": "object"}, lambda _args: None))

    app.ctx.events.emit("test", "demo.happened", {"level": 7})
    app.ctx.events.emit("test", "demo.happened", {"level": 1})
    assert app.process_events() == 2

    assert notes == ["level 7"]
    pending = app.approvals.list("pending")
    assert [request.tool for request in pending] == ["demo_act"]
    assert pending[0].origin == "rule"
    fired = [record for record in app.ctx.audit.records() if record["kind"] == "rule.fired"]
    assert [record["status"] for record in fired] == ["ok", "pending"]
