"""Offline task tool, approval and reminder-rule flows; no real delivery."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.tasks import PAST, configured

pytestmark = pytest.mark.integration


def test_task_tools_register_without_network_or_queued_reminders(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app, service = configured(make_app)
    names = {tool.name for tool in app.registry.all()}
    assert {"tasks_add", "tasks_update", "tasks_read", "tasks_list", "tasks_status", "tasks_delete"} <= names
    assert service.status({})["counts"] == {"open": 0, "completed": 0, "cancelled": 0}
    assert app.ctx.events.pending() == []
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "args",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"title": "Example", "notes": "x" * 4001},
        {"title": "Example", "unexpected": True},
        {"title": "Example", "remind_at": True},
    ],
)
def test_task_input_schema_enforces_real_bounds(make_app: AppFactory, args: dict[str, Any]) -> None:
    app, _service = configured(make_app)
    assert app.call("tasks_add", args, Origin.ASSISTANT).status == "invalid"
    assert app.call("tasks_list", {}, Origin.ASSISTANT).data["tasks"] == []


def test_ambiguous_date_is_a_visible_tool_error_not_a_silent_timezone(make_app: AppFactory) -> None:
    app, _service = configured(make_app)
    outcome = app.call("tasks_add", {"title": "Example", "due_at": "2026-11-12T10:00:00"}, Origin.ASSISTANT)
    assert outcome.status == "error"
    assert "timezone" in outcome.message
    assert app.call("tasks_list", {}, Origin.ASSISTANT).data["tasks"] == []


def test_assistant_delete_waits_for_exact_revision_approval(make_app: AppFactory) -> None:
    app, _service = configured(make_app)
    task = app.call("tasks_add", {"title": "Example"}, Origin.ASSISTANT).data
    pending = app.call("tasks_delete", {"id": task["id"], "revision": 1}, Origin.ASSISTANT)
    assert pending.status == "pending"
    assert pending.approval_id is not None
    assert app.call("tasks_read", {"id": task["id"]}, Origin.ASSISTANT).status == "ok"
    assert app.dispatcher.approve(pending.approval_id).status == "ok"
    assert app.call("tasks_read", {"id": task["id"]}, Origin.ASSISTANT).status == "error"
    assert app.dispatcher.approve(pending.approval_id).status == "error"


def test_edit_after_delete_request_blocks_stale_approval(make_app: AppFactory) -> None:
    app, _service = configured(make_app)
    task = app.call("tasks_add", {"title": "Before"}, Origin.ASSISTANT).data
    pending = app.call("tasks_delete", {"id": task["id"], "revision": 1}, Origin.ASSISTANT)
    assert pending.approval_id is not None
    assert (
        app.call("tasks_update", {"id": task["id"], "revision": 1, "title": "After"}, Origin.ASSISTANT).status
        == "ok"
    )
    result = app.dispatcher.approve(pending.approval_id)
    assert result.status == "error"
    assert "changed" in result.message
    assert app.call("tasks_read", {"id": task["id"]}, Origin.ASSISTANT).data["title"] == "After"


def test_reminder_is_consumed_by_rules_but_is_not_itself_delivery(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    captured: list[dict[str, Any]] = []
    app.registry.add(
        Tool(
            "test_capture_reminder",
            "Capture synthetic reminder data.",
            Tier.DRAFT,
            {
                "type": "object",
                "properties": {"title": {"type": "string", "maxLength": 200}},
                "required": ["title"],
                "additionalProperties": False,
            },
            lambda args: captured.append(args),
        )
    )
    from switchboard.rules import RuleEngine, load_rules

    app.rules = RuleEngine(
        load_rules(
            [
                {
                    "name": "Synthetic reminder capture",
                    "on": "tasks.reminder_due",
                    "do": [{"tool": "test_capture_reminder", "args": {"title": "{payload.title}"}}],
                }
            ]
        ),
        app.dispatcher,
        app.ctx.audit,
    )
    service.add({"title": "Example reminder", "remind_at": PAST})
    service.reminders_job()
    assert captured == []
    app.process_events()
    assert captured == [{"title": "Example reminder"}]
    service.reminders_job()
    app.process_events()
    assert len(captured) == 1


def test_cancelling_cannot_withdraw_an_already_queued_event(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Example reminder", "remind_at": PAST})
    service.reminders_job()
    service.update({"id": task["id"], "revision": 1, "status": "cancelled"})
    assert len(app.ctx.events.recent(event_type="tasks.reminder_due")) == 1
    assert service.read({"id": task["id"]})["status"] == "cancelled"
