"""Dispatcher and approvals: the full path every call takes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from switchboard.app import App
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory

SEND_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "maxLength": 100},
        "text": {"type": "string", "maxLength": 200},
    },
    "required": ["to", "text"],
    "additionalProperties": False,
}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.fixture
def app_and_outbox(make_app: AppFactory) -> tuple[App, list[dict[str, Any]]]:
    app = make_app({"policy": {"tools": {"demo_send": {"allow_recipients": ["*@example.com"]}}}})
    outbox: list[dict[str, Any]] = []

    def send(args: dict[str, Any]) -> dict[str, Any]:
        outbox.append(args)
        return {"sent": True}

    def boom(_args: dict[str, Any]) -> None:
        raise RuntimeError("secret detail that must not leak")

    app.registry.add(
        Tool(
            "demo_send",
            "Send a demo message.",
            Tier.ACT,
            SEND_SCHEMA,
            send,
            recipients=lambda args: [args["to"]],
            summarize=lambda args: f"send a message to {args['to']}",
        )
    )
    app.registry.add(Tool("demo_read", "Read demo data.", Tier.READ, EMPTY, lambda _args: {"value": 42}))
    app.registry.add(Tool("demo_boom", "Always fails.", Tier.READ, EMPTY, boom))
    return app, outbox


def test_read_tools_run_immediately(app_and_outbox: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_outbox
    outcome = app.call("demo_read", {}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.data == {"value": 42}


def test_act_tools_wait_for_approval_and_run_the_exact_arguments(
    app_and_outbox: tuple[App, list[dict[str, Any]]],
) -> None:
    app, outbox = app_and_outbox
    outcome = app.call("demo_send", {"to": "pat@example.com", "text": "hi"}, Origin.ASSISTANT)
    assert outcome.status == "pending"
    assert outcome.approval_id is not None
    assert outbox == []

    status = app.call("approvals_status", {"id": outcome.approval_id}, Origin.ASSISTANT)
    assert status.data["status"] == "pending"

    executed = app.dispatcher.approve(outcome.approval_id)
    assert executed.status == "ok"
    assert outbox == [{"to": "pat@example.com", "text": "hi"}]
    stored = app.approvals.get(outcome.approval_id)
    assert stored is not None
    assert stored.status == "executed"
    assert stored.result == {"sent": True}

    again = app.dispatcher.approve(outcome.approval_id)
    assert again.status == "error"
    assert "executed" in again.message
    assert len(outbox) == 1


def test_recipient_outside_the_allowlist_is_denied_without_asking(
    app_and_outbox: tuple[App, list[dict[str, Any]]],
) -> None:
    app, outbox = app_and_outbox
    outcome = app.call("demo_send", {"to": "pat@example.org", "text": "hi"}, Origin.ASSISTANT)
    assert outcome.status == "denied"
    assert "allowlist" in outcome.message
    assert app.approvals.list("pending") == []
    assert outbox == []


def test_denying_and_direct_human_calls(app_and_outbox: tuple[App, list[dict[str, Any]]]) -> None:
    app, outbox = app_and_outbox
    pending = app.call("demo_send", {"to": "pat@example.com", "text": "hi"}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert app.dispatcher.deny(pending.approval_id)
    stored = app.approvals.get(pending.approval_id)
    assert stored is not None and stored.status == "denied"
    assert not app.dispatcher.deny(pending.approval_id)

    direct = app.call("demo_send", {"to": "pat@example.com", "text": "direct"}, Origin.HUMAN)
    assert direct.status == "ok"
    assert outbox[-1]["text"] == "direct"


def test_invalid_arguments_and_unknown_tools(app_and_outbox: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_outbox
    invalid = app.call("demo_send", {"to": "pat@example.com"}, Origin.ASSISTANT)
    assert invalid.status == "invalid"
    assert "text" in invalid.message
    assert app.call("no_such_tool", {}, Origin.ASSISTANT).status == "invalid"


def test_unexpected_errors_report_only_the_exception_type(
    app_and_outbox: tuple[App, list[dict[str, Any]]],
) -> None:
    app, _ = app_and_outbox
    outcome = app.call("demo_boom", {}, Origin.ASSISTANT)
    assert outcome.status == "error"
    assert "RuntimeError" in outcome.message
    assert "secret detail" not in outcome.message
    assert all("secret detail" not in json.dumps(record) for record in app.ctx.audit.records())


def test_expired_approvals_cannot_run(app_and_outbox: tuple[App, list[dict[str, Any]]]) -> None:
    app, outbox = app_and_outbox
    pending = app.call("demo_send", {"to": "pat@example.com", "text": "late"}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    with app.ctx.db.session() as conn:
        conn.execute(
            "UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (pending.approval_id,),
        )
    outcome = app.dispatcher.approve(pending.approval_id)
    assert outcome.status == "error"
    assert "expired" in outcome.message
    assert outbox == []


def test_every_call_is_audited(app_and_outbox: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_outbox
    app.call("demo_read", {}, Origin.ASSISTANT)
    kinds = [record["kind"] for record in app.ctx.audit.records()]
    assert kinds[-2:] == ["tool.request", "tool.done"]
    assert app.ctx.audit.verify().ok


def test_rate_limits_count_only_successful_calls(make_app: AppFactory) -> None:
    app = make_app({"policy": {"tools": {"demo_read": {"max_per_hour": 2}}}})
    app.registry.add(Tool("demo_read", "Read.", Tier.READ, EMPTY, lambda _args: 1))
    statuses = [app.call("demo_read", {}, Origin.ASSISTANT).status for _ in range(3)]
    assert statuses == ["ok", "ok", "denied"]


def test_approval_summaries_cannot_carry_terminal_escape_sequences(make_app: AppFactory) -> None:
    app = make_app()
    app.registry.add(
        Tool(
            "demo_act",
            "Act.",
            Tier.ACT,
            EMPTY,
            lambda _args: None,
            summarize=lambda _args: "send to friend@example.com\x1b[8m hidden\u202e",
        )
    )
    outcome = app.call("demo_act", {}, Origin.ASSISTANT)
    assert outcome.approval_id is not None
    stored = app.approvals.get(outcome.approval_id)
    assert stored is not None
    assert "\x1b" not in stored.summary and "\u202e" not in stored.summary
    assert "\\x1b[8m" in stored.summary
