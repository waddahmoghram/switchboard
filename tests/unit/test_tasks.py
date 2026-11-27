"""Task revisions, timestamp interpretation and reminder atomicity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from switchboard.connectors.tasks.service import normalize
from switchboard.connectors.tasks.settings import load_settings
from switchboard.errors import ConfigError, ToolError
from tests.conftest import AppFactory
from tests.fixtures.tasks import FUTURE, NOW, PAST, configured


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": True},
        {"poll_interval_seconds": True},
        {"poll_interval_seconds": 29},
        {"poll_interval_seconds": 3601},
        {"poll_interval_seconds": "60"},
        {"max_reminders_per_tick": False},
        {"max_reminders_per_tick": 0},
        {"max_reminders_per_tick": 501},
    ],
)
def test_settings_refuse_typos_booleans_and_invalid_limits(settings: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(settings)


def test_settings_defaults_and_valid_bounds() -> None:
    assert load_settings({}).poll_interval_seconds == 60
    assert (
        load_settings({"poll_interval_seconds": 30, "max_reminders_per_tick": 500}).max_reminders_per_tick
        == 500
    )


@pytest.mark.parametrize("value", ["2026-11-12", "2026-11-12T12:00:00", "tomorrow", "", "invalid+00:00"])
def test_ambiguous_or_invalid_dates_are_refused(value: str) -> None:
    with pytest.raises(ToolError, match="explicit timezone"):
        normalize({"remind_at": value})


def test_offsets_are_normalized_and_null_dates_are_preserved() -> None:
    assert normalize({"due_at": "2026-11-12T04:00:00-08:00"})["due_at"] == "2026-11-12T12:00:00+00:00"
    assert normalize({"due_at": None}) == {"due_at": None}


@pytest.mark.parametrize("field", ["title", "list_name"])
def test_blank_names_are_refused(field: str) -> None:
    with pytest.raises(ToolError, match="blank"):
        normalize({field: " \n "})


def test_create_and_read_keep_full_notes_and_hide_internal_keys(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": " Example task ", "notes": "private example", "list_name": " home "})
    assert task["title"] == "Example task"
    assert task["list_name"] == "home"
    assert task["revision"] == 1
    assert task["status"] == "open"
    assert task["reminder_delivery"] == "not_scheduled"
    assert task["untrusted_content"] is True
    assert "reminder_key" not in task
    assert "search_text" not in task
    assert service.read({"id": task["id"]})["notes"] == "private example"


def test_list_previews_and_literal_search_are_bounded(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    first = service.add({"title": "Literal 10%_ example", "notes": "x" * 1000})
    service.add({"title": "Another example", "notes": "ordinary"})
    assert len(service.listing({"query": "%_"})["tasks"]) == 1
    assert service.listing({"query": "missing"})["tasks"] == []
    preview = service.listing({"query": "literal"})["tasks"][0]
    assert preview["id"] == first["id"]
    assert len(preview["notes"]) <= 180
    assert preview["notes_truncated"] is True


def test_pagination_is_by_stable_increasing_identifiers(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    ids = [service.add({"title": f"Example {index}"})["id"] for index in range(3)]
    page = service.listing({"limit": 2})
    assert [task["id"] for task in page["tasks"]] == ids[:2]
    assert page["truncated"] is True
    assert page["next_after_id"] == ids[1]
    last = service.listing({"after_id": page["next_after_id"], "limit": 2})
    assert [task["id"] for task in last["tasks"]] == ids[2:]
    assert last["truncated"] is False
    assert last["next_after_id"] is None


def test_list_and_overdue_filters_do_not_include_completed_tasks(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    overdue = service.add({"title": "Overdue example", "due_at": PAST, "list_name": "home"})
    future = service.add({"title": "Future example", "due_at": FUTURE, "list_name": "home"})
    service.add({"title": "Other list", "due_at": PAST, "list_name": "other"})
    assert [item["id"] for item in service.listing({"overdue": True, "list_name": "home"})["tasks"]] == [
        overdue["id"]
    ]
    service.update({"id": overdue["id"], "revision": 1, "status": "completed"})
    assert service.listing({"overdue": True, "list_name": "home"})["tasks"] == []
    assert service.listing({"list_name": "home"})["tasks"][0]["id"] == future["id"]


def test_update_requires_current_revision_and_at_least_one_field(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Original"})
    updated = service.update({"id": task["id"], "revision": 1, "title": "New title"})
    assert updated["revision"] == 2
    assert updated["title"] == "New title"
    with pytest.raises(ToolError, match="current revision"):
        service.update({"id": task["id"], "revision": 1, "title": "Stale edit"})
    with pytest.raises(ToolError, match="at least one"):
        service.update({"id": task["id"], "revision": 2})
    assert len(app.ctx.events.recent(event_type="tasks.changed")) == 2


def test_concurrent_edits_have_one_winner(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": "Original"})

    def edit(title: str) -> str:
        try:
            service.update({"id": task["id"], "revision": 1, "title": title})
        except ToolError:
            return "stale"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["First", "Second"])) == ["stale", "updated"]
    assert service.read({"id": task["id"]})["revision"] == 2


def test_due_reminder_queues_once_not_a_delivery_receipt(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Example reminder", "due_at": FUTURE, "remind_at": PAST})
    assert service.store.queue_due(NOW, 100) == 1
    assert service.store.queue_due(NOW, 100) == 0
    [event] = app.ctx.events.recent(event_type="tasks.reminder_due")
    assert event.payload["id"] == task["id"]
    assert event.payload["title"] == "Example reminder"
    assert event.payload["delivery"] == "event_queued"
    assert service.read({"id": task["id"]})["reminder_delivery"] == "event_queued"


def test_unrelated_edits_do_not_resend_a_queued_reminder(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": "Before", "remind_at": PAST})
    service.store.queue_due(NOW, 100)
    service.update({"id": task["id"], "revision": 1, "title": "After", "due_at": FUTURE})
    assert service.store.queue_due(NOW, 100) == 0


def test_rescheduling_is_a_new_reminder_and_clearing_it_cancels_future_queueing(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Example", "remind_at": PAST})
    service.store.queue_due(NOW, 100)
    service.update({"id": task["id"], "revision": 1, "remind_at": "2026-11-12T11:30:00+00:00"})
    assert service.store.queue_due(NOW, 100) == 1
    assert len(app.ctx.events.recent(event_type="tasks.reminder_due")) == 2
    service.update({"id": task["id"], "revision": 2, "remind_at": None})
    assert service.read({"id": task["id"]})["reminder_delivery"] == "not_scheduled"
    assert service.store.queue_due(NOW, 100) == 0


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_closed_tasks_do_not_queue_future_reminders(make_app: AppFactory, status: str) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": "Example", "remind_at": PAST})
    service.update({"id": task["id"], "revision": 1, "status": status})
    assert service.store.queue_due(NOW, 100) == 0


def test_reopening_does_not_repeat_an_already_queued_reminder(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": "Example", "remind_at": PAST})
    service.store.queue_due(NOW, 100)
    service.update({"id": task["id"], "revision": 1, "status": "completed"})
    service.update({"id": task["id"], "revision": 2, "status": "open"})
    assert service.store.queue_due(NOW, 100) == 0


def test_backlog_is_bounded_per_scheduler_tick(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    for index in range(3):
        service.add({"title": f"Example {index}", "remind_at": PAST})
    assert service.store.queue_due(NOW, 1) == 1
    assert service.store.queue_due(NOW, 1) == 1
    assert len(app.ctx.events.recent(event_type="tasks.reminder_due")) == 2


def test_reminder_mark_and_event_roll_back_together(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Example", "remind_at": PAST})

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic event failure")

    with monkeypatch.context() as patch:
        patch.setattr(app.ctx.events, "emit", fail)
        with pytest.raises(RuntimeError, match="event failure"):
            service.store.queue_due(NOW, 100)
    assert service.read({"id": task["id"]})["reminder_delivery"] == "not_queued"
    assert app.ctx.events.recent(event_type="tasks.reminder_due") == []
    assert service.store.queue_due(NOW, 100) == 1


def test_failed_change_event_does_not_save_a_partial_edit(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = configured(make_app)
    task = service.add({"title": "Before"})

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(app.ctx.events, "emit", fail)
    with pytest.raises(RuntimeError):
        service.update({"id": task["id"], "revision": 1, "title": "After"})
    assert service.read({"id": task["id"]})["title"] == "Before"


def test_deleted_identifiers_are_not_reused_and_missing_items_fail(make_app: AppFactory) -> None:
    _app, service = configured(make_app)
    task = service.add({"title": "First"})
    assert service.delete({"id": task["id"], "revision": 1})["status"] == "deleted"
    with pytest.raises(ToolError, match="no local task"):
        service.read({"id": task["id"]})
    assert service.add({"title": "Next"})["id"] > task["id"]


def test_runner_errors_are_explicit_in_status(make_app: AppFactory) -> None:
    app, service = configured(make_app)
    next(job for job in app.ctx.scheduler.jobs() if job.name == "tasks.reminders").last_error = "RuntimeError"
    assert service.status({})["job_error"] == "RuntimeError"


def test_last_check_survives_a_separate_assistant_process(make_app: AppFactory) -> None:
    _app, runner = configured(make_app)
    runner.add({"title": "Example", "remind_at": PAST})
    runner.reminders_job()
    _second_app, assistant = configured(make_app)
    assert assistant.status({})["last_check"] == {
        "checked_at": "2026-11-12T12:00:00+00:00",
        "queued": 1,
        "error": None,
    }


def test_expected_reminder_job_failure_is_durable_and_reraised(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, service = configured(make_app)

    def fail(_moment: object, _maximum: object) -> int:
        raise ToolError("synthetic queue failure")

    monkeypatch.setattr(service.store, "queue_due", fail)
    with pytest.raises(ToolError, match="queue failure"):
        service.reminders_job()
    assert service.status({})["last_check"]["error"] == "ToolError"


@pytest.mark.parametrize(
    "raw",
    [
        '{"unrelated": true}',
        '{"checked_at": null, "queued": 0, "error": null}',
        '{"checked_at": "yesterday", "queued": 0, "error": null}',
        '{"checked_at": "2026-11-12T12:00:00+00:00", "queued": true, "error": null}',
        '{"checked_at": "2026-11-12T12:00:00+00:00", "queued": null, "error": null}',
    ],
)
def test_damaged_reminder_metadata_is_not_presented_as_success(make_app: AppFactory, raw: str) -> None:
    app, service = configured(make_app)
    app.ctx.db.kv_set("tasks", "reminders", raw)
    with pytest.raises(ToolError, match="damaged"):
        service.status({})
