"""End-to-end data lifecycle: backups taken while records are appended, rotation under concurrent appends, and
what retention does and does not reach. Temporary directories only; authored, not run on the authoring host.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from switchboard import lifecycle
from switchboard.app import App, build_app
from switchboard.config import config_from_dict
from tests.conftest import AppFactory

pytestmark = pytest.mark.integration

OLD = "2020-01-01T00:00:00+00:00"


def _append_in_background(app: App, count: int) -> threading.Thread:
    started = threading.Event()

    def append() -> None:
        started.set()
        for index in range(count):
            app.ctx.audit.append("test.concurrent", index=index)

    thread = threading.Thread(target=append)
    thread.start()
    assert started.wait(timeout=10)
    return thread


def test_a_backup_taken_during_audit_appends_is_consistent(make_app: AppFactory, tmp_path: Path) -> None:
    app = make_app()
    app.ctx.audit.append("test.before")
    thread = _append_in_background(app, 200)
    report = lifecycle.create_backup(app, tmp_path / "backup")
    thread.join(timeout=60)

    verified = lifecycle.verify_backup(report.path)
    assert verified.ok, verified.problems
    assert verified.audit_records == report.manifest["audit"]["records"]
    assert 1 <= report.manifest["audit"]["records"] <= 201
    assert app.ctx.audit.verify().ok


def test_rotation_during_concurrent_appends_loses_no_record(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.audit.append("test.before")
    thread = _append_in_background(app, 200)
    lifecycle.rotate_audit(app)
    thread.join(timeout=60)

    chain = lifecycle.verify_audit_chain(app.ctx.audit.path, app.ctx.data_dir / "audit-archive")

    assert chain.ok, chain.problems
    assert len(chain.segments) == 2
    assert chain.records == 1 + 200 + 1


def test_backups_keep_what_retention_later_removes(make_app: AppFactory, tmp_path: Path) -> None:
    settings = {"switchboard": {"connectors": ["tasks"]}, "retention": {"connectors": {"tasks": 30}}}
    app = make_app(settings)
    with app.ctx.db.session() as conn:
        conn.execute(
            "INSERT INTO tasks_items (revision, list_name, title, notes, search_text, status, created_at, "
            "updated_at) VALUES (1, 'personal', 'Archived chore', '', 'archived chore', 'completed', ?, ?)",
            (OLD, OLD),
        )
    backup = lifecycle.create_backup(app, tmp_path / "backup").path

    purged = lifecycle.purge_data(app, dry_run=False)

    assert purged.connectors == {"tasks": {"tasks_items": 1}}
    assert lifecycle.verify_backup(backup).ok
    target = tmp_path / "restored"
    lifecycle.restore_backup(backup, target)
    restored = build_app(config_from_dict(settings, target))
    export = lifecycle.export_data(restored, tmp_path / "export.json")
    tasks = json.loads(export.path.read_text(encoding="ascii"))["connectors"]["tasks"]["tasks_items"]
    assert [task["title"] for task in tasks] == ["Archived chore"]
