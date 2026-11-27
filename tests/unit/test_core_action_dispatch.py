"""Dispatcher and action journal: result classification, the duplicate guard, approvals, and the envelope."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from switchboard import execution
from switchboard.actions import ActionJournal
from switchboard.app import App
from switchboard.errors import HttpError, ToolError, UncertainOutcomeError
from switchboard.mcp_server import McpServer
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory

TEXT: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 100}},
    "required": ["text"],
    "additionalProperties": False,
}
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def scripted(calls: list[dict[str, Any]]) -> Callable[[dict[str, Any]], Any]:
    """A handler whose behaviour is the part of ``text`` before the first colon."""

    def handle(args: dict[str, Any]) -> Any:
        calls.append(args)
        mode = args["text"].split(":")[0]
        if mode == "unknown":
            return {"effect_unknown": True, "status": "requested"}
        if mode == "stringly":
            return {"effect_unknown": "true"}
        if mode == "nested":
            return {"report": {"effect_unknown": True}}
        if mode == "none":
            return None
        if mode == "uncertain":
            raise UncertainOutcomeError("the device stopped answering after the command was sent")
        if mode == "refused":
            raise ToolError("the device refused the command")
        if mode == "http":
            raise HttpError("POST device.example: connection reset")
        if mode == "crash":
            raise RuntimeError("secret detail that must not leak")
        return {"sent": True}

    return handle


@pytest.fixture
def app_and_calls(make_app: AppFactory) -> tuple[App, list[dict[str, Any]]]:
    app = make_app({"switchboard": {"tool_timeout_seconds": 30}})
    calls: list[dict[str, Any]] = []
    app.registry.add(
        Tool(
            "demo_write",
            "Write.",
            Tier.ACT,
            TEXT,
            scripted(calls),
            summarize=lambda args: f"write {args['text']}",
            connector="demo",
        )
    )
    app.registry.add(Tool("demo_draft", "Draft.", Tier.DRAFT, TEXT, scripted(calls), connector="demo"))
    app.registry.add(Tool("demo_read", "Read.", Tier.READ, TEXT, scripted(calls), connector="demo"))
    return app, calls


@pytest.mark.parametrize(
    ("text", "status", "state"),
    [
        ("ok", "ok", "succeeded"),
        ("none", "ok", "succeeded"),
        ("stringly", "ok", "succeeded"),
        ("nested", "ok", "succeeded"),
        ("unknown", "ok", "uncertain"),
        ("uncertain", "error", "uncertain"),
        ("refused", "error", "failed"),
        ("http", "error", "uncertain"),
        ("crash", "error", "uncertain"),
    ],
)
def test_every_result_is_classified_and_journaled(
    app_and_calls: tuple[App, list[dict[str, Any]]], text: str, status: str, state: str
) -> None:
    app, calls = app_and_calls
    outcome = app.call("demo_write", {"text": text}, Origin.HUMAN)
    assert outcome.status == status
    assert outcome.action is not None
    assert outcome.action["state"] == state
    assert re.fullmatch(r"[0-9a-f]{32}", outcome.action["id"])
    assert outcome.to_dict()["action"] == {"id": outcome.action["id"], "state": state}
    record = app.journal.get(outcome.action["id"])
    assert record is not None
    assert (record.state, record.tool, record.origin, record.args) == (
        state,
        "demo_write",
        "human",
        {"text": text},
    )
    assert record.finished_at is not None
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["unknown", "uncertain", "http"])
def test_uncertain_outcomes_tell_the_assistant_how_the_user_reconciles(
    app_and_calls: tuple[App, list[dict[str, Any]]], text: str
) -> None:
    app, _ = app_and_calls
    outcome = app.call("demo_write", {"text": text}, Origin.HUMAN)
    assert outcome.action is not None
    assert f"switchboard actions resolve {outcome.action['id']}" in outcome.message


def test_succeeded_and_failed_outcomes_keep_their_plain_messages(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, _ = app_and_calls
    assert app.call("demo_write", {"text": "ok"}, Origin.HUMAN).message == ""
    assert (
        app.call("demo_write", {"text": "refused"}, Origin.HUMAN).message == "the device refused the command"
    )


def test_read_calls_are_not_journaled_and_may_be_repeated(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, calls = app_and_calls
    failed = app.call("demo_read", {"text": "crash"}, Origin.ASSISTANT)
    assert failed.status == "error"
    assert failed.action is None
    assert "action" not in failed.to_dict()
    assert app.call("demo_read", {"text": "crash"}, Origin.ASSISTANT).status == "error"
    ok = app.call("demo_read", {"text": "unknown"}, Origin.ASSISTANT)
    assert ok.status == "ok"
    assert ok.action is None
    assert ok.message == ""
    assert len(calls) == 3
    assert app.journal.recent() == []


def test_draft_calls_are_journaled_with_their_origin(app_and_calls: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_calls
    outcome = app.call("demo_draft", {"text": "ok"}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.action is not None
    record = app.journal.get(outcome.action["id"])
    assert record is not None
    assert (record.origin, record.approval_id) == ("assistant", None)


def test_calls_refused_before_execution_have_no_action(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, calls = app_and_calls
    pending = app.call("demo_write", {"text": "ok"}, Origin.ASSISTANT)
    assert pending.status == "pending"
    assert pending.action is None
    invalid = app.call("demo_write", {"text": 5}, Origin.HUMAN)
    assert invalid.status == "invalid"
    assert invalid.action is None
    app.policy.kill_switch.touch()
    denied = app.call("demo_write", {"text": "ok"}, Origin.HUMAN)
    assert denied.status == "denied"
    assert denied.action is None
    assert calls == []
    assert app.journal.recent() == []


def test_an_uncertain_write_blocks_an_identical_repeat_until_reconciled(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, calls = app_and_calls
    first = app.call("demo_write", {"text": "uncertain"}, Origin.HUMAN)
    assert first.action is not None
    assert first.action["state"] == "uncertain"

    repeat = app.call("demo_write", {"text": "uncertain"}, Origin.HUMAN)

    assert repeat.status == "denied"
    assert repeat.action is None
    assert first.action["id"] in repeat.message
    assert len(calls) == 1
    denial = [record for record in app.ctx.audit.records() if record["kind"] == "tool.denied"][-1]
    assert (denial["action"], denial["action_state"]) == (first.action["id"], "uncertain")
    assert app.call("demo_write", {"text": "ok"}, Origin.HUMAN).status == "ok"
    assert app.call("demo_draft", {"text": "uncertain"}, Origin.HUMAN).status == "error"

    app.journal.reconcile(first.action["id"], applied=False, note="nothing reached the device")
    after = app.call("demo_write", {"text": "uncertain"}, Origin.HUMAN)
    assert after.status == "error"
    assert after.action is not None
    assert after.action["state"] == "uncertain"
    assert len(calls) == 4


def test_the_hourly_budget_still_counts_only_ok_and_running_calls(make_app: AppFactory) -> None:
    app = make_app({"policy": {"tools": {"demo_write": {"max_per_hour": 2}}}})
    calls: list[dict[str, Any]] = []
    app.registry.add(Tool("demo_write", "Write.", Tier.ACT, TEXT, scripted(calls)))
    texts = ["uncertain", "uncertain", "ok", "ok:2", "ok:3"]

    statuses = [app.call("demo_write", {"text": text}, Origin.HUMAN).status for text in texts]

    assert statuses == ["error", "denied", "ok", "ok", "denied"]
    with app.ctx.db.session() as conn:
        rows = conn.execute("SELECT outcome FROM tool_calls WHERE tool = 'demo_write' ORDER BY id").fetchall()
    assert [row["outcome"] for row in rows] == ["error", "denied", "ok", "ok", "denied"]
    assert len(calls) == 3


def test_approved_executions_link_the_approval_to_its_action(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, _ = app_and_calls
    pending = app.call("demo_write", {"text": "unknown"}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    before = app.call("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT).data
    assert before["status"] == "pending"
    assert "action" not in before

    executed = app.dispatcher.approve(pending.approval_id)

    assert executed.status == "ok"
    assert executed.action is not None
    assert executed.action["state"] == "uncertain"
    record = app.journal.get(executed.action["id"])
    assert record is not None
    assert (record.origin, record.approval_id) == ("approval", pending.approval_id)
    status = app.call("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT).data
    assert status["status"] == "executed"
    assert status["action"] == {"id": executed.action["id"], "state": "uncertain"}


def test_an_approval_meeting_the_duplicate_guard_finishes_failed(
    app_and_calls: tuple[App, list[dict[str, Any]]],
) -> None:
    app, calls = app_and_calls
    first = app.call("demo_write", {"text": "uncertain"}, Origin.HUMAN)
    assert first.action is not None
    pending = app.call("demo_write", {"text": "uncertain"}, Origin.ASSISTANT)
    assert pending.status == "pending"
    assert pending.approval_id is not None

    blocked = app.dispatcher.approve(pending.approval_id)

    assert blocked.status == "denied"
    assert blocked.action is None
    stored = app.approvals.get(pending.approval_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error is not None
    assert first.action["id"] in stored.error
    status = app.call("approvals_status", {"id": pending.approval_id}, Origin.ASSISTANT).data
    assert status["status"] == "failed"
    assert "action" not in status
    assert len(calls) == 1


def test_the_envelope_reaches_mcp_structured_content(make_app: AppFactory) -> None:
    app = make_app({"policy": {"tools": {"demo_write": {"decision": "allow"}}}})
    calls: list[dict[str, Any]] = []
    app.registry.add(Tool("demo_write", "Write.", Tier.ACT, TEXT, scripted(calls)))
    app.registry.add(Tool("demo_read", "Read.", Tier.READ, TEXT, scripted(calls)))
    server = McpServer(tools=app.registry.all, call=app.dispatcher.call)

    def call(name: str, text: str) -> dict[str, Any]:
        params = {"_meta": MODERN, "name": name, "arguments": {"text": text}}
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        assert reply is not None
        result: dict[str, Any] = reply["result"]
        return result

    written = call("demo_write", "unknown")
    content = written["structuredContent"]
    assert content["status"] == "ok"
    assert written["isError"] is False
    assert content["action"]["state"] == "uncertain"
    assert len(content["action"]["id"]) == 32
    assert json.loads(written["content"][0]["text"])["action"] == content["action"]
    refused = call("demo_write", "unknown")
    assert refused["isError"] is True
    assert "action" not in refused["structuredContent"]
    assert "action" not in call("demo_read", "ok")["structuredContent"]


def test_actions_recent_lists_public_entries(app_and_calls: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_calls
    app.call("demo_write", {"text": "ok"}, Origin.HUMAN)
    uncertain = app.call("demo_write", {"text": "uncertain"}, Origin.HUMAN)
    assert uncertain.action is not None

    listing = app.call("actions_recent", {"state": "uncertain"}, Origin.ASSISTANT)

    assert listing.status == "ok"
    assert listing.action is None
    [entry] = listing.data["actions"]
    assert (entry["id"], entry["state"], entry["tool"]) == (uncertain.action["id"], "uncertain", "demo_write")
    assert "args" not in entry
    assert "owner" not in entry
    assert listing.data["open"] == {"in_flight": 0, "uncertain": 1}
    assert listing.data["reconciled_by"] == "operator only"
    assert len(app.call("actions_recent", {}, Origin.ASSISTANT).data["actions"]) == 2
    assert len(app.call("actions_recent", {"limit": 1}, Origin.ASSISTANT).data["actions"]) == 1
    for bad in ({"limit": 0}, {"limit": 101}, {"state": "done"}, {"id": "x"}):
        assert app.call("actions_recent", bad, Origin.ASSISTANT).status == "invalid"
    tool = app.registry.get("actions_recent")
    assert tool is not None
    assert tool.tier is Tier.READ


def test_operator_reconciliation_is_never_a_tool(app_and_calls: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_calls
    names = {tool.name for tool in app.registry.all()}
    assert not any("resolve" in name or "reconcile" in name for name in names)


def test_unexpected_exception_text_is_not_journaled(app_and_calls: tuple[App, list[dict[str, Any]]]) -> None:
    app, _ = app_and_calls
    outcome = app.call("demo_write", {"text": "crash"}, Origin.HUMAN)
    assert outcome.action is not None
    record = app.journal.get(outcome.action["id"])
    assert record is not None
    assert record.error is not None
    assert "RuntimeError" in record.error
    assert "secret detail" not in record.error
    assert "secret detail" not in outcome.message
    error = [item for item in app.ctx.audit.records() if item["kind"] == "tool.error"][-1]
    assert (error["action"], error["action_state"]) == (outcome.action["id"], "uncertain")
    assert app.ctx.audit.verify().ok


def test_handlers_run_under_the_tool_deadline(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"tool_timeout_seconds": 30}})
    seen: list[execution.Deadline | None] = []
    app.registry.add(
        Tool("demo_write", "Write.", Tier.ACT, TEXT, lambda _args: seen.append(execution.current()))
    )

    outcome = app.call("demo_write", {"text": "ok"}, Origin.HUMAN)

    [limit] = seen
    assert limit is not None
    assert (limit.label, limit.seconds) == ("tool demo_write", 30)
    assert execution.current() is None
    assert outcome.action is not None
    record = app.journal.get(outcome.action["id"])
    assert record is not None
    started = datetime.fromisoformat(record.started_at)
    assert datetime.fromisoformat(record.deadline_at) - started == timedelta(seconds=30)


def test_handlers_hold_their_connector_lane(make_app: AppFactory) -> None:
    app = make_app()
    probes: list[bool] = []

    def handler(_args: dict[str, Any]) -> dict[str, Any]:
        def probe() -> None:
            with app.dispatcher.lanes.hold("demo", timeout=0) as held:
                probes.append(held)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(5)
        with app.dispatcher.lanes.hold("demo", timeout=0) as again:
            probes.append(again)
        return {"done": True}

    app.registry.add(Tool("demo_write", "Write.", Tier.ACT, TEXT, handler, connector="demo"))
    assert app.call("demo_write", {"text": "ok"}, Origin.HUMAN).status == "ok"
    assert probes == [False, True]
    assert app.ctx.scheduler.lanes is app.dispatcher.lanes


def test_a_lane_stuck_past_its_holders_deadline_refuses_work_without_an_action(
    app_and_calls: tuple[App, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, calls = app_and_calls
    holding = threading.Event()
    release = threading.Event()

    def stuck_job() -> None:
        with execution.deadline(5, "job demo.poll"), app.dispatcher.lanes.hold("demo"):
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=stuck_job)
    worker.start()
    assert holding.wait(5)
    real = execution.monotonic
    monkeypatch.setattr(execution, "monotonic", lambda: real() + 3600)
    try:
        outcome = app.call("demo_write", {"text": "ok"}, Origin.HUMAN)
    finally:
        release.set()
        worker.join(5)
    assert outcome.status == "error"
    assert "busy" in outcome.message
    assert outcome.action is None
    assert calls == []
    assert app.journal.recent() == []


def test_build_and_tick_sweep_abandoned_attempts(make_app: AppFactory) -> None:
    app = make_app()
    past = datetime.now(UTC) - timedelta(hours=1)
    dead = ActionJournal(app.ctx.db, owner="ended-process", clock=lambda: past)
    first = dead.begin(
        tool="demo_write", args={"text": "a"}, origin="rule", approval_id=None, timeout_seconds=60
    )
    assert app.journal.get(first.id) is not None

    app.tick()

    swept = app.journal.get(first.id)
    assert swept is not None
    assert swept.state == "uncertain"
    second = dead.begin(
        tool="demo_write", args={"text": "b"}, origin="rule", approval_id=None, timeout_seconds=60
    )
    make_app()
    rebuilt = app.journal.get(second.id)
    assert rebuilt is not None
    assert rebuilt.state == "uncertain"


def test_an_unrecordable_result_stays_in_flight_for_the_sweep(
    app_and_calls: tuple[App, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_and_calls

    def broken(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.journal, "finish", broken)
    outcome = app.call("demo_write", {"text": "ok"}, Origin.HUMAN)
    assert outcome.status == "ok"
    assert outcome.action is not None
    assert outcome.action["state"] == "in_flight"
    assert "switchboard actions resolve" in outcome.message


def test_rule_audit_records_name_the_action(make_app: AppFactory) -> None:
    app = make_app(
        {"rules": [{"name": "note", "on": "demo.*", "do": [{"tool": "demo_draft", "args": {"text": "ok"}}]}]}
    )
    calls: list[dict[str, Any]] = []
    app.registry.add(Tool("demo_draft", "Draft.", Tier.DRAFT, TEXT, scripted(calls)))
    app.ctx.events.emit("test", "demo.happened", {})

    assert app.process_events() == 1

    [fired] = [record for record in app.ctx.audit.records() if record["kind"] == "rule.fired"]
    assert fired["status"] == "ok"
    record = app.journal.get(fired["action"])
    assert record is not None
    assert record.origin == "rule"
