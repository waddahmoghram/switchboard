"""Connector retention hooks: opt-in purges keep open work, dry runs keep everything, and each hook follows
the contract. Rows are inserted directly with synthetic values.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard import lifecycle
from switchboard.app import App
from switchboard.connectors.finance import retention as finance_retention
from switchboard.connectors.linkedin import retention as linkedin_retention
from switchboard.connectors.shopping import retention as shopping_retention
from switchboard.connectors.tasks import retention as tasks_retention
from switchboard.connectors.telegram import retention as telegram_retention
from switchboard.connectors.webhooks import retention as webhooks_retention
from switchboard.connectors.whatsapp import retention as whatsapp_retention
from switchboard.errors import ToolError
from switchboard.util import iso, now_iso
from tests.conftest import AppFactory
from tests.fixtures.telegram import TOKEN
from tests.fixtures.telegram import config as telegram_config

OLD = "2020-01-01T00:00:00+00:00"
CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)
HOOKS = ("finance", "linkedin", "shopping", "tasks", "telegram", "webhooks", "whatsapp")


def _rows(app: App, query: str) -> list[tuple[Any, ...]]:
    with app.ctx.db.session() as conn:
        return [tuple(row) for row in conn.execute(query).fetchall()]


def _dry_then_apply(
    app: App, purge: Callable[..., dict[str, int]], query: str
) -> tuple[dict[str, int], list[tuple[Any, ...]]]:
    """Run a dry run, check it changed nothing, then apply and check both report the same counts."""
    before = _rows(app, query)
    dry = purge(app.ctx, older_than=CUTOFF, dry_run=True)
    assert _rows(app, query) == before
    applied = purge(app.ctx, older_than=CUTOFF, dry_run=False)
    assert applied == dry
    return applied, _rows(app, query)


@pytest.mark.parametrize("name", HOOKS)
def test_every_hook_follows_the_contract(name: str) -> None:
    module = lifecycle.retention_hook(name)
    assert module is not None
    parameters = inspect.signature(module.purge).parameters
    assert list(parameters) == ["ctx", "older_than", "dry_run"]
    assert parameters["older_than"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["dry_run"].kind is inspect.Parameter.KEYWORD_ONLY
    for table in lifecycle.export_tables(name, module):
        assert table.startswith(f"{name}_")


def test_connectors_without_a_hook_report_none() -> None:
    assert lifecycle.retention_hook("calendar") is None
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.retention_hook("nonesuch")


def test_telegram_keeps_replies_in_flight_or_of_unknown_outcome(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_TELEGRAM_BOT", TOKEN)
    app = make_app(telegram_config())
    states = [
        ("new", OLD),
        ("sent", OLD),
        ("failed", OLD),
        ("sending", OLD),
        ("unknown", OLD),
        ("new", now_iso()),
    ]
    with app.ctx.db.session() as conn:
        for message_id, (state, received_at) in enumerate(states, start=1):
            conn.execute(
                "INSERT INTO telegram_messages (bot_id, update_id, chat_id, message_id, user_id, chat_type, "
                "text, truncated, message_date, received_at, reply_state) "
                "VALUES (777, ?, 111, ?, 111, 'private', 'synthetic text', 0, ?, ?, ?)",
                (100 + message_id, message_id, received_at, received_at, state),
            )

    counts, rows = _dry_then_apply(
        app, telegram_retention.purge, "SELECT reply_state, received_at FROM telegram_messages ORDER BY id"
    )

    assert counts == {"telegram_messages": 3}
    assert [state for state, _received in rows] == ["sending", "unknown", "new"]


def test_tasks_purges_only_closed_tasks(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["tasks"]}})
    recent = now_iso()
    with app.ctx.db.session() as conn:
        for title, status, updated_at in [
            ("old completed", "completed", OLD),
            ("old cancelled", "cancelled", OLD),
            ("old open", "open", OLD),
            ("recent completed", "completed", recent),
        ]:
            conn.execute(
                "INSERT INTO tasks_items (revision, list_name, title, notes, search_text, status, "
                "created_at, updated_at) VALUES (1, 'personal', ?, '', ?, ?, ?, ?)",
                (title, title, status, updated_at, updated_at),
            )

    counts, rows = _dry_then_apply(app, tasks_retention.purge, "SELECT title FROM tasks_items ORDER BY id")

    assert counts == {"tasks_items": 2}
    assert rows == [("old open",), ("recent completed",)]
    assert _rows(app, "SELECT type FROM events WHERE type = 'tasks.deleted'") == []


def test_webhooks_purges_processed_bodies_and_expired_replay_entries(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["webhooks"]}})
    recent = now_iso()
    with app.ctx.db.session() as conn:
        conn.executemany(
            "INSERT INTO events (source, type, payload_json, dedupe_key, created_at, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("webhooks", "webhook.received", '{"json": "old"}', None, OLD, OLD),
                ("webhooks", "webhook.received", '{"json": "deduplicated"}', "webhooks:h:1", OLD, OLD),
                ("webhooks", "webhook.received", '{"json": "unprocessed"}', None, OLD, None),
                ("webhooks", "webhook.received", '{"json": "recent"}', None, recent, recent),
                ("mail", "mail.received", '{"text": "other source"}', None, OLD, OLD),
            ],
        )
        expired = int((CUTOFF - timedelta(days=1)).timestamp())
        live = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
        conn.executemany(
            "INSERT INTO webhooks_seen (hook, replay_key, expires_at) VALUES ('h', ?, ?)",
            [("expired", expired), ("live", live)],
        )

    counts, rows = _dry_then_apply(
        app, webhooks_retention.purge, "SELECT source, payload_json, dedupe_key FROM events ORDER BY id"
    )

    assert counts == {"webhook_events_deleted": 1, "webhook_events_emptied": 1, "webhooks_seen": 1}
    assert rows == [
        ("webhooks", "{}", "webhooks:h:1"),
        ("webhooks", '{"json": "unprocessed"}', None),
        ("webhooks", '{"json": "recent"}', None),
        ("mail", '{"text": "other source"}', None),
    ]
    assert _rows(app, "SELECT replay_key FROM webhooks_seen") == [("live",)]


def _inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "statements"
    for folder in (inbox, inbox / "processed", inbox / "failed"):
        folder.mkdir()
        folder.chmod(0o700)
    return inbox


def test_finance_removes_only_old_statement_files(make_app: AppFactory, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = make_app(
        {"switchboard": {"connectors": ["finance"]}, "connectors": {"finance": {"import_dir": str(inbox)}}}
    )
    outside = tmp_path / "outside.ofx"
    outside.write_text("not in the inbox", encoding="utf-8")
    files = {
        "processed/statement.ofx": "remove",
        "processed/recent.csv": "keep",
        "processed/notes.txt": "keep",
        "processed/.hidden.ofx": "keep",
        "failed/broken.qfx": "remove",
    }
    for name in files:
        (inbox / name).write_text("synthetic statement", encoding="utf-8")
    future = (datetime.now(UTC) + timedelta(days=10)).timestamp()
    os.utime(inbox / "processed" / "recent.csv", (future, future))
    (inbox / "processed" / "linked.ofx").symlink_to(outside)
    # Every file was just created, so its status-change time is now: a cutoff tomorrow makes it eligible.
    tomorrow = datetime.now(UTC) + timedelta(days=1)

    dry = finance_retention.purge(app.ctx, older_than=tomorrow, dry_run=True)
    assert all((inbox / name).exists() for name in files)
    applied = finance_retention.purge(app.ctx, older_than=tomorrow, dry_run=False)

    assert dry == applied == {"processed_files": 1, "failed_files": 1, "files_not_removed": 0}
    for name, expected in files.items():
        assert (inbox / name).exists() is (expected == "keep")
    assert (inbox / "processed" / "linked.ofx").is_symlink()
    assert outside.read_text(encoding="utf-8") == "not in the inbox"


def test_finance_without_an_inbox_does_nothing_and_an_unsafe_inbox_is_refused(
    make_app: AppFactory, tmp_path: Path
) -> None:
    plain = make_app({"switchboard": {"connectors": ["finance"]}})
    assert finance_retention.purge(plain.ctx, older_than=CUTOFF, dry_run=False) == {
        "processed_files": 0,
        "failed_files": 0,
        "files_not_removed": 0,
    }
    inbox = _inbox(tmp_path)
    inbox.chmod(0o777)
    unsafe = make_app(
        {"switchboard": {"connectors": ["finance"]}, "connectors": {"finance": {"import_dir": str(inbox)}}}
    )
    with pytest.raises(ToolError, match="writable by other users"):
        finance_retention.purge(unsafe.ctx, older_than=CUTOFF, dry_run=True)


def test_shopping_keeps_each_watchs_newest_observation(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["shopping"]}})
    recent = now_iso()
    with app.ctx.db.session() as conn:
        for watch_id in (1, 2):
            conn.execute(
                "INSERT INTO shopping_watches (id, url, target_price, currency, created_at, updated_at) "
                "VALUES (?, ?, '10.00', 'USD', ?, ?)",
                (watch_id, f"https://shop.example/item-{watch_id}", OLD, OLD),
            )
        conn.executemany(
            "INSERT INTO shopping_prices (watch_id, observed_at, price, currency, source) "
            "VALUES (?, ?, ?, 'USD', 'synthetic')",
            [
                (1, OLD, "12.00"),
                (1, OLD, "11.00"),
                (1, OLD, "10.50"),
                (2, OLD, "20.00"),
                (2, recent, "19.00"),
            ],
        )

    counts, rows = _dry_then_apply(
        app, shopping_retention.purge, "SELECT watch_id, price FROM shopping_prices ORDER BY id"
    )

    assert counts == {"shopping_prices": 3}
    assert rows == [(1, "10.50"), (2, "19.00")]


@pytest.mark.parametrize(
    ("name", "purge", "settings"),
    [
        ("linkedin", linkedin_retention.purge, {}),
        ("whatsapp", whatsapp_retention.purge, {"date_order": "DMY"}),
    ],
)
def test_imported_snapshots_are_purged_by_import_age(
    make_app: AppFactory, name: str, purge: Callable[..., dict[str, int]], settings: dict[str, Any]
) -> None:
    app = make_app({"switchboard": {"connectors": [name]}, "connectors": {name: settings}})
    recent = now_iso()
    with app.ctx.db.session() as conn:
        for key, imported_at in (("old", OLD), ("recent", recent)):
            # The table prefix is the connector name from the fixed parameter list above.
            conn.execute(
                f"INSERT INTO {name}_imports (file_key, filename, records, imported_at) "  # noqa: S608
                "VALUES (?, ?, 1, ?)",
                (f"{key}-file", f"{key}.zip", imported_at),
            )
            conn.execute(
                f"INSERT INTO {name}_records (record_key, kind, label, data_json, search_text, "  # noqa: S608
                "imported_at) VALUES (?, 'message', 'Example', '{}', '', ?)",
                (f"{key}-record", imported_at),
            )

    counts, rows = _dry_then_apply(app, purge, f"SELECT record_key FROM {name}_records")  # noqa: S608

    assert counts == {f"{name}_records": 1, f"{name}_imports": 1}
    assert rows == [("recent-record",)]
    assert _rows(app, f"SELECT file_key FROM {name}_imports") == [("recent-file",)]  # noqa: S608


def test_linkedin_drafts_are_never_purged(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["linkedin"]}})
    with app.ctx.db.session() as conn:
        conn.execute(
            "INSERT INTO linkedin_drafts (id, text, visibility, author_urn, api_version, draft_hash, "
            "created_at) VALUES ('draft-1', 'synthetic', 'CONNECTIONS', '', '202610', ?, ?)",
            ("0" * 64, OLD),
        )
    linkedin_retention.purge(app.ctx, older_than=CUTOFF, dry_run=False)
    assert _rows(app, "SELECT id FROM linkedin_drafts") == [("draft-1",)]


def test_hooks_compare_against_the_given_cutoff_not_the_current_time(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"connectors": ["tasks"]}})
    moment = iso(CUTOFF - timedelta(days=1))
    with app.ctx.db.session() as conn:
        conn.execute(
            "INSERT INTO tasks_items (revision, list_name, title, notes, search_text, status, created_at, "
            "updated_at) VALUES (1, 'personal', 'closed', '', 'closed', 'completed', ?, ?)",
            (moment, moment),
        )
    assert tasks_retention.purge(app.ctx, older_than=CUTOFF - timedelta(days=2), dry_run=True) == {
        "tasks_items": 0
    }
    assert tasks_retention.purge(app.ctx, older_than=CUTOFF, dry_run=True) == {"tasks_items": 1}
