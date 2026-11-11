"""Durable event queue: deduplication, delivery order, error isolation, derived events, and pruning."""

from __future__ import annotations

from switchboard.events import Event
from tests.conftest import AppFactory


def test_emit_deduplicates_and_delivers_in_order(make_app: AppFactory) -> None:
    app = make_app()
    first = app.ctx.events.emit("test", "demo.one", {"n": 1}, dedupe_key="k1")
    duplicate = app.ctx.events.emit("test", "demo.one", {"n": 1}, dedupe_key="k1")
    assert first is not None
    assert duplicate is None

    seen: list[int] = []
    app.ctx.events.subscribe("demo.*", "recorder", lambda event: seen.append(event.payload["n"]))
    app.ctx.events.emit("test", "demo.two", {"n": 2})
    assert app.ctx.events.process() == 2
    assert seen == [1, 2]
    assert app.ctx.events.pending() == []


def test_failing_subscribers_are_recorded_not_fatal(make_app: AppFactory) -> None:
    app = make_app()

    def broken(_event: Event) -> None:
        raise ZeroDivisionError

    calls: list[int] = []
    app.ctx.events.subscribe("demo.*", "broken", broken)
    app.ctx.events.subscribe("demo.*", "working", lambda event: calls.append(event.id))
    event_id = app.ctx.events.emit("test", "demo.x", {})
    app.ctx.events.process()
    assert calls == [event_id]
    with app.ctx.db.session() as conn:
        row = conn.execute("SELECT error FROM events WHERE id = ?", (event_id,)).fetchone()
    assert "broken: ZeroDivisionError" in row["error"]
    [failure] = app.ctx.events.failures()
    assert failure["id"] == event_id
    assert failure["error"] == "broken: ZeroDivisionError"
    assert "payload" not in failure and "payload_json" not in failure


def test_derived_events_are_processed_in_later_rounds(make_app: AppFactory) -> None:
    app = make_app()
    seen: list[str] = []

    def derive(event: Event) -> None:
        seen.append(event.type)
        if event.type == "demo.first":
            app.ctx.events.emit("test", "demo.second", {})

    app.ctx.events.subscribe("demo.*", "deriver", derive)
    app.ctx.events.emit("test", "demo.first", {})
    assert app.ctx.events.process() == 2
    assert seen == ["demo.first", "demo.second"]


def test_prune_removes_only_old_processed_events(make_app: AppFactory) -> None:
    app = make_app()
    old = app.ctx.events.emit("test", "demo.old", {})
    app.ctx.events.process()
    with app.ctx.db.session() as conn:
        conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (old,))
    keep = app.ctx.events.emit("test", "demo.new", {})
    assert app.ctx.events.prune(14) == 1
    assert [event.id for event in app.ctx.events.recent()] == [keep]


def test_recent_events_can_be_filtered(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.events.emit("mail", "mail.received", {})
    wanted = app.ctx.events.emit("webhooks", "webhook.received", {})
    app.ctx.events.emit("webhooks", "webhook.other", {})
    filtered = app.ctx.events.recent(event_type="webhook.received", source="webhooks")
    assert [event.id for event in filtered] == [wanted]


def test_prune_keeps_deduplication_keys_but_drops_old_payloads(make_app: AppFactory) -> None:
    app = make_app()
    keyed = app.ctx.events.emit("test", "demo.keyed", {"secret": "old data"}, dedupe_key="msg-1")
    app.ctx.events.process()
    with app.ctx.db.session() as conn:
        conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (keyed,))
    assert app.ctx.events.prune(14) == 1
    assert app.ctx.events.emit("test", "demo.keyed", {"secret": "old data"}, dedupe_key="msg-1") is None
    assert [event.payload for event in app.ctx.events.recent()] == [{}]


def test_kv_setdefault_keeps_the_first_value(make_app: AppFactory) -> None:
    app = make_app()
    assert app.ctx.db.kv_setdefault("test", "salt", "first") == "first"
    assert app.ctx.db.kv_setdefault("test", "salt", "second") == "first"
