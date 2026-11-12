"""Data lifecycle: backups with tamper detection, restore safety, private exports, retention, and audit
archives. Uses temporary directories and synthetic data; authored, not run on the authoring host.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard import lifecycle
from switchboard.actions import ActionJournal
from switchboard.app import App, build_app
from switchboard.audit import GENESIS
from switchboard.config import config_from_dict
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from switchboard.util import now_iso, sha256_hex
from tests.conftest import AppFactory

OLD = "2000-01-01T00:00:00+00:00"
FAR = "2999-01-01T00:00:00+00:00"
SEND_SCHEMA = {
    "type": "object",
    "properties": {"to": {"type": "string", "maxLength": 80}},
    "required": ["to"],
    "additionalProperties": False,
}


def _with_history(make_app: AppFactory) -> App:
    app = make_app()
    app.ctx.audit.append("test.one", value=1)
    app.ctx.audit.append("test.two", value=2)
    app.ctx.events.emit("test", "demo.event", {"n": 1})
    return app


def _backup(app: App, tmp_path: Path, name: str = "backup") -> Path:
    return lifecycle.create_backup(app, tmp_path / name).path


def _manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    return manifest


def _rewrite_manifest(path: Path, manifest: dict[str, Any]) -> None:
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _rehash(path: Path, name: str) -> None:
    """Make the manifest agree with an edited file, as a careful tamperer would."""
    manifest = _manifest(path)
    body = (path / name).read_bytes()
    manifest["files"][name] = {"sha256": sha256_hex(body), "bytes": len(body)}
    _rewrite_manifest(path, manifest)


def _problems(path: Path, expect_audit_head: str | None = None) -> str:
    result = lifecycle.verify_backup(path, expect_audit_head=expect_audit_head)
    assert not result.ok
    return "\n".join(result.problems)


def _kinds(app: App) -> list[str]:
    return [str(record["kind"]) for record in app.ctx.audit.records()]


# ---- backups ----------------------------------------------------------------------------------------


def test_a_backup_is_private_complete_and_verifies(make_app: AppFactory, tmp_path: Path) -> None:
    app = _with_history(make_app)
    head = app.ctx.audit.head()

    report = lifecycle.create_backup(app, tmp_path / "backup")

    assert report.path.stat().st_mode & 0o777 == 0o700
    assert sorted(entry.name for entry in report.path.iterdir()) == [
        "audit.jsonl",
        "manifest.json",
        "switchboard.sqlite3",
    ]
    for entry in report.path.iterdir():
        assert entry.stat().st_mode & 0o777 == 0o600
    manifest = _manifest(report.path)
    assert manifest == report.manifest
    assert manifest["format"] == "switchboard-backup"
    assert manifest["audit"] == {"head": head, "records": 2}
    assert manifest["schema_versions"]["core"] == lifecycle.known_schema_versions()["core"]
    assert manifest["kill_switch"] is False
    assert set(manifest["files"]) == {"switchboard.sqlite3", "audit.jsonl"}
    assert report.manifest_sha256 == sha256_hex((report.path / "manifest.json").read_bytes())
    assert report.problems == ()

    verified = lifecycle.verify_backup(report.path, expect_audit_head=head)
    assert verified.ok, verified.problems
    assert verified.audit_head == head
    assert _kinds(app)[-1] == "backup.created"


def test_a_backup_never_reuses_a_directory(make_app: AppFactory, tmp_path: Path) -> None:
    (tmp_path / "backup").mkdir()
    with pytest.raises(lifecycle.LifecycleError, match="already exists"):
        lifecycle.create_backup(make_app(), tmp_path / "backup")


def test_a_changed_database_copy_fails_verification(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    database = path / "switchboard.sqlite3"
    data = bytearray(database.read_bytes())
    data[-1] ^= 0xFF
    database.write_bytes(bytes(data))
    assert "switchboard.sqlite3 does not match the hash" in _problems(path)


def test_an_edited_manifest_head_fails_verification(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    manifest = _manifest(path)
    manifest["audit"]["head"] = "f" * 64
    _rewrite_manifest(path, manifest)
    assert "differs from the manifest" in _problems(path)


def test_a_rewritten_audit_copy_breaks_its_chain_even_with_a_matching_hash(
    make_app: AppFactory, tmp_path: Path
) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    audit = path / "audit.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["value"] = 999
    lines[0] = json.dumps(record)
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rehash(path, "audit.jsonl")
    assert "audit.jsonl: record 1: content does not match its hash" in _problems(path)


def test_planted_missing_and_linked_files_fail_verification(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    (path / "notes.txt").write_text("planted", encoding="utf-8")
    assert "unexpected entry notes.txt" in _problems(path)
    (path / "notes.txt").unlink()
    moved = tmp_path / "elsewhere.jsonl"
    (path / "audit.jsonl").rename(moved)
    assert "audit.jsonl is missing" in _problems(path)
    (path / "audit.jsonl").symlink_to(moved)
    assert "unexpected entry audit.jsonl" in _problems(path)


@pytest.mark.parametrize(
    ("component", "version", "message"),
    [("core", 99, "newer than this Switchboard understands"), ("mystery", 1, "not recognized")],
)
def test_newer_or_unknown_schema_versions_fail_verification(
    make_app: AppFactory, tmp_path: Path, component: str, version: int, message: str
) -> None:
    path = _backup(make_app(), tmp_path)
    with closing(sqlite3.connect(path / "switchboard.sqlite3")) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (component, version, applied_at) VALUES (?, ?, ?)",
            (component, version, OLD),
        )
        conn.commit()
    manifest = _manifest(path)
    manifest["schema_versions"][component] = version
    _rewrite_manifest(path, manifest)
    _rehash(path, "switchboard.sqlite3")
    assert message in _problems(path)


def test_an_independently_recorded_audit_head_is_compared(make_app: AppFactory, tmp_path: Path) -> None:
    app = _with_history(make_app)
    head = app.ctx.audit.head()
    path = _backup(app, tmp_path)
    assert lifecycle.verify_backup(path, expect_audit_head=head).ok
    assert "independently recorded head" in _problems(path, expect_audit_head="e" * 64)
    assert "64 lowercase hexadecimal" in _problems(path, expect_audit_head="not-a-hash")


def test_backups_carry_audit_archives_and_check_their_links(make_app: AppFactory, tmp_path: Path) -> None:
    app = _with_history(make_app)
    rotation = lifecycle.rotate_audit(app)
    app.ctx.audit.append("test.after", value=3)
    path = _backup(app, tmp_path)
    archive = f"audit-archive/{rotation.archive.name}"
    assert archive in _manifest(path)["files"]
    assert lifecycle.verify_backup(path).ok

    copied = path / archive
    copied.write_text(copied.read_text(encoding="utf-8").replace('"value":1', '"value":7'), encoding="utf-8")
    _rehash(path, archive)
    assert "does not match the digest recorded" in _problems(path)


def test_a_missing_backup_or_manifest_is_reported_not_raised(tmp_path: Path) -> None:
    assert "does not exist" in _problems(tmp_path / "absent")
    (tmp_path / "empty").mkdir()
    assert "manifest.json is missing" in _problems(tmp_path / "empty")
    (tmp_path / "empty" / "manifest.json").write_text('{"format": "something-else"}', encoding="utf-8")
    assert "does not describe a Switchboard backup" in _problems(tmp_path / "empty")


# ---- restore ----------------------------------------------------------------------------------------


def test_restore_rebuilds_a_working_data_directory_with_the_kill_switch_on(
    make_app: AppFactory, tmp_path: Path
) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    head = _manifest(path)["audit"]["head"]
    target = tmp_path / "restored"

    report = lifecycle.restore_backup(path, target)

    assert report.target == target
    assert report.audit_head == head
    assert target.stat().st_mode & 0o777 == 0o700
    restored = build_app(config_from_dict({}, target))
    assert restored.policy.kill_switch.exists()
    assert restored.ctx.audit.verify().ok
    records = list(restored.ctx.audit.records())
    assert [record["kind"] for record in records] == ["test.one", "test.two", "backup.restored"]
    assert records[-1]["prev"] == head
    assert [event.type for event in restored.ctx.events.recent(5)] == ["demo.event"]


def test_restore_refuses_a_directory_that_is_not_empty(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    target = tmp_path / "busy"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="not empty"):
        lifecycle.restore_backup(path, target)
    assert sorted(entry.name for entry in target.iterdir()) == ["keep.txt"]


def test_restore_refuses_while_a_runner_holds_the_target(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    fd = os.open(target / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(lifecycle.LifecycleError, match="runner"):
            lifecycle.restore_backup(path, target)
    finally:
        os.close(fd)
    assert sorted(entry.name for entry in target.iterdir()) == ["runner.lock"]
    lifecycle.restore_backup(path, target)
    assert (target / "switchboard.sqlite3").exists()


def test_restore_refuses_a_backup_that_does_not_verify(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(_with_history(make_app), tmp_path)
    (path / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="does not verify"):
        lifecycle.restore_backup(path, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_restore_tightens_an_existing_empty_directory(make_app: AppFactory, tmp_path: Path) -> None:
    path = _backup(make_app(), tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    lifecycle.restore_backup(path, target)
    assert target.stat().st_mode & 0o777 == 0o700


# ---- export -----------------------------------------------------------------------------------------


def test_exports_are_private_and_include_arguments_but_no_key_value_contents(
    make_app: AppFactory, tmp_path: Path
) -> None:
    app = make_app()
    app.registry.add(Tool("demo_send", "Send a synthetic message.", Tier.ACT, SEND_SCHEMA, lambda args: args))
    pending = app.dispatcher.call("demo_send", {"to": "a@example.invalid"}, Origin.ASSISTANT)
    journal = ActionJournal(app.ctx.db)
    attempt = journal.begin(
        tool="demo_send",
        args={"to": "b@example.invalid"},
        origin="human",
        approval_id=None,
        timeout_seconds=30,
    )
    journal.finish(attempt.id, "succeeded")
    app.ctx.events.emit("test", "demo.event", {"n": 1})
    app.ctx.db.kv_set("demo", "refresh", "kv-value-that-must-not-be-exported")

    report = lifecycle.export_data(app, tmp_path / "export.json")

    assert report.path.stat().st_mode & 0o777 == 0o600
    text = report.path.read_text(encoding="ascii")
    assert "kv-value-that-must-not-be-exported" not in text
    exported = json.loads(text)
    assert exported["format"] == "switchboard-export"
    assert [(item["id"], item["args"]) for item in exported["approvals"]] == [
        (pending.approval_id, {"to": "a@example.invalid"})
    ]
    assert [item["args"] for item in exported["actions"]] == [{"to": "b@example.invalid"}]
    assert "owner" not in exported["actions"][0]
    assert [event["payload"] for event in exported["events"]] == [{"n": 1}]
    assert exported["tool_calls"]
    [demo] = [item for item in exported["kv_namespaces"] if item["namespace"] == "demo"]
    assert demo["keys"] == 1
    assert report.counts["approvals"] == 1
    assert report.problems == ()
    assert _kinds(app)[-1] == "data.exported"


def test_exports_never_overwrite_a_file(make_app: AppFactory, tmp_path: Path) -> None:
    destination = tmp_path / "export.json"
    destination.write_text("keep", encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="already exists"):
        lifecycle.export_data(make_app(), destination)
    assert destination.read_text(encoding="utf-8") == "keep"


def test_exports_include_tables_that_enabled_connectors_offer(make_app: AppFactory, tmp_path: Path) -> None:
    app = make_app({"switchboard": {"connectors": ["tasks"]}})
    stamp = now_iso()
    with app.ctx.db.session() as conn:
        conn.execute(
            "INSERT INTO tasks_items (revision, list_name, title, notes, search_text, status, created_at, "
            "updated_at) VALUES (1, 'personal', 'Water the plants', '', 'water the plants', 'open', ?, ?)",
            (stamp, stamp),
        )

    report = lifecycle.export_data(app, tmp_path / "export.json")

    assert report.connectors == {"tasks": {"tasks_items": 1}}
    exported = json.loads(report.path.read_text(encoding="ascii"))
    assert exported["connectors"]["tasks"]["tasks_items"][0]["title"] == "Water the plants"


# ---- retention --------------------------------------------------------------------------------------


def _seed(app: App) -> None:
    recent = now_iso()
    with app.ctx.db.session() as conn:
        conn.executemany(
            "INSERT INTO tool_calls (tool, origin, decision, outcome, created_at) "
            "VALUES (?, 'assistant', 'allow', ?, ?)",
            [("old_ok", "ok", OLD), ("old_running", "running", OLD), ("recent_ok", "ok", recent)],
        )
        conn.executemany(
            "INSERT INTO approvals (id, tool, args_json, summary, origin, status, created_at, expires_at, "
            "decided_at) VALUES (?, 'demo_send', '{}', 'synthetic', 'assistant', ?, ?, ?, ?)",
            [
                ("0000000a", "executed", OLD, OLD, OLD),
                ("0000000b", "pending", OLD, FAR, None),
                ("0000000c", "approved", OLD, OLD, OLD),
                ("0000000d", "denied", recent, FAR, recent),
                ("0000000e", "expired", OLD, OLD, None),
                ("0000000f", "pending", OLD, OLD, None),
            ],
        )
        conn.executemany(
            "INSERT INTO action_journal (id, tool, args_digest, args_json, origin, state, owner, started_at, "
            "deadline_at, finished_at, resolved_at) VALUES (?, 'demo_send', ?, '{}', 'assistant', ?, 'test', "
            "?, ?, ?, ?)",
            [
                ("a" * 32, "0" * 64, "succeeded", OLD, OLD, OLD, None),
                ("b" * 32, "1" * 64, "uncertain", OLD, OLD, OLD, None),
                ("c" * 32, "2" * 64, "in_flight", OLD, FAR, None, None),
                ("d" * 32, "3" * 64, "reconciled_not_applied", OLD, OLD, OLD, OLD),
                ("e" * 32, "4" * 64, "failed", recent, FAR, recent, None),
            ],
        )
        conn.executemany(
            "INSERT INTO events (source, type, payload_json, dedupe_key, created_at, processed_at) "
            "VALUES ('test', 'demo.event', ?, ?, ?, ?)",
            [
                ('{"old": "processed"}', None, OLD, OLD),
                ('{"old": "deduplicated"}', "test:kept-key", OLD, OLD),
                ('{"old": "unprocessed"}', None, OLD, None),
                ('{"recent": "processed"}', None, recent, recent),
            ],
        )


def _snapshot(app: App) -> dict[str, Any]:
    with app.ctx.db.session() as conn:
        return {
            "tool_calls": sorted(row[0] for row in conn.execute("SELECT tool FROM tool_calls")),
            "approvals": sorted(row[0] for row in conn.execute("SELECT id FROM approvals")),
            "actions": sorted(row[0] for row in conn.execute("SELECT state FROM action_journal")),
            "events": sorted(row[0] for row in conn.execute("SELECT payload_json FROM events")),
        }


def test_a_dry_run_counts_exactly_what_an_apply_removes(make_app: AppFactory) -> None:
    app = make_app()
    _seed(app)
    before = _snapshot(app)

    dry = lifecycle.purge_data(app, dry_run=True)

    assert dry.counts == {"tool_calls": 1, "approvals": 3, "actions": 2, "events": 2}
    assert _snapshot(app) == before
    assert "data.purged" not in _kinds(app)

    applied = lifecycle.purge_data(app, dry_run=False)

    assert applied.counts == dry.counts
    assert _snapshot(app) == {
        "tool_calls": ["old_running", "recent_ok"],
        "approvals": ["0000000b", "0000000c", "0000000d"],
        "actions": ["failed", "in_flight", "uncertain"],
        "events": sorted(['{"old": "unprocessed"}', '{"recent": "processed"}', "{}"]),
    }
    assert _kinds(app)[-1] == "data.purged"


def test_open_journal_entries_survive_any_retention_period(make_app: AppFactory) -> None:
    app = make_app({"retention": {"actions_days": 1}})
    _seed(app)
    lifecycle.purge_data(app, dry_run=False)
    with app.ctx.db.session() as conn:
        states = {row[0] for row in conn.execute("SELECT state FROM action_journal")}
    assert {"in_flight", "uncertain"} <= states


def _old_closed_task(app: App) -> None:
    with app.ctx.db.session() as conn:
        conn.execute(
            "INSERT INTO tasks_items (revision, list_name, title, notes, search_text, status, created_at, "
            "updated_at) VALUES (1, 'personal', 'Done long ago', '', 'done long ago', 'completed', ?, ?)",
            (OLD, OLD),
        )


def _tasks(app: App) -> int:
    with app.ctx.db.session() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks_items").fetchone()[0])


def test_connector_retention_needs_an_explicit_entry_and_an_enabled_connector(make_app: AppFactory) -> None:
    plain = make_app({"switchboard": {"connectors": ["tasks"]}})
    _old_closed_task(plain)
    assert lifecycle.purge_data(plain, dry_run=False).connectors == {}
    assert _tasks(plain) == 1

    opted_in = make_app(
        {
            "switchboard": {"connectors": ["tasks"]},
            "retention": {"connectors": {"tasks": 30, "telegram": 14, "nonesuch": 5}},
        }
    )
    report = lifecycle.purge_data(opted_in, dry_run=False)

    assert report.connectors == {"tasks": {"tasks_items": 1}}
    assert report.skipped == {"telegram": "not enabled"}
    assert report.problems == ("[retention.connectors] nonesuch is not a known connector",)
    assert "connector:tasks" in report.cutoffs
    assert _tasks(opted_in) == 0


def test_missing_failing_and_malformed_hooks_are_reported(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app({"switchboard": {"connectors": ["tasks"]}, "retention": {"connectors": {"tasks": 30}}})
    _seed(app)

    monkeypatch.setattr(lifecycle, "retention_hook", lambda name: None)
    assert "tasks has no retention hook" in lifecycle.purge_data(app, dry_run=True).problems[0]

    def explode(ctx: Any, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(lifecycle, "retention_hook", lambda name: SimpleNamespace(purge=explode))
    report = lifecycle.purge_data(app, dry_run=True)
    assert "the retention hook failed (RuntimeError)" in report.problems[0]
    assert report.counts["tool_calls"] == 1

    def negative(ctx: Any, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
        return {"rows": -1}

    monkeypatch.setattr(lifecycle, "retention_hook", lambda name: SimpleNamespace(purge=negative))
    assert "whole numbers" in lifecycle.purge_data(app, dry_run=True).problems[0]


def test_purge_transactions_roll_back_dry_runs_and_failures(make_app: AppFactory) -> None:
    app = make_app()
    insert = "INSERT INTO kv (namespace, key, value, updated_at) VALUES ('t', ?, 'v', ?)"

    def rows() -> int:
        with app.ctx.db.session() as reader:
            return int(reader.execute("SELECT COUNT(*) FROM kv WHERE namespace = 't'").fetchone()[0])

    with app.ctx.db.session() as conn:
        with lifecycle.purge_transaction(conn, dry_run=True):
            conn.execute(insert, ("dry", OLD))
        assert rows() == 0
        with pytest.raises(RuntimeError), lifecycle.purge_transaction(conn, dry_run=False):
            conn.execute(insert, ("failed", OLD))
            raise RuntimeError("abort")
        assert rows() == 0
        with lifecycle.purge_transaction(conn, dry_run=False):
            conn.execute(insert, ("kept", OLD))
    assert rows() == 1


def test_known_schema_versions_cover_the_core_and_migrating_connectors() -> None:
    versions = lifecycle.known_schema_versions()
    assert versions["core"] >= 2
    assert versions["tasks"] >= 1
    assert "shelly" not in versions


# ---- audit archives ---------------------------------------------------------------------------------


def test_rotation_archives_privately_and_links_the_new_chain(make_app: AppFactory) -> None:
    app = make_app()
    for index in range(3):
        app.ctx.audit.append("test.event", index=index)
    head = app.ctx.audit.head()

    report = lifecycle.rotate_audit(app)

    archive_dir = app.ctx.data_dir / "audit-archive"
    assert archive_dir.stat().st_mode & 0o777 == 0o700
    assert report.archive.parent == archive_dir
    assert report.archive.stat().st_mode & 0o777 == 0o600
    assert (report.previous_head, report.records) == (head, 3)
    assert report.archive_sha256 == sha256_hex(report.archive.read_bytes())
    [first] = list(app.ctx.audit.records())
    assert first["kind"] == "audit.rotated"
    assert first["prev"] == GENESIS
    assert (first["previous_head"], first["archive"], first["hash"]) == (
        head,
        report.archive.name,
        report.head,
    )
    assert app.ctx.audit.verify().ok
    assert app.ctx.audit.append("test.after")["prev"] == report.head

    chain = lifecycle.verify_audit_chain(app.ctx.audit.path, archive_dir)
    assert chain.ok, chain.problems
    assert [segment.records for segment in chain.segments] == [2, 3]
    assert chain.records == 5


def test_rotating_twice_links_both_archives(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.audit.append("test.first")
    first = lifecycle.rotate_audit(app, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    app.ctx.audit.append("test.second")
    second = lifecycle.rotate_audit(app, clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))

    chain = lifecycle.verify_audit_chain(app.ctx.audit.path, app.ctx.data_dir / "audit-archive")

    assert chain.ok, chain.problems
    assert [segment.name for segment in chain.segments] == [
        "audit.jsonl",
        second.archive.name,
        first.archive.name,
    ]
    assert [segment.records for segment in chain.segments] == [1, 2, 1]


def test_a_changed_or_missing_archive_breaks_the_linked_chain(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.audit.append("test.event", value=1)
    report = lifecycle.rotate_audit(app)
    archive_dir = report.archive.parent

    report.archive.write_bytes(report.archive.read_bytes().replace(b'"value":1', b'"value":2'))
    changed = lifecycle.verify_audit_chain(app.ctx.audit.path, archive_dir)
    assert not changed.ok
    assert "does not match the digest" in changed.problems[0]

    report.archive.unlink()
    missing = lifecycle.verify_audit_chain(app.ctx.audit.path, archive_dir)
    assert "which is missing" in missing.problems[0]


def test_rotation_refuses_an_empty_or_broken_log(make_app: AppFactory) -> None:
    app = make_app()
    with pytest.raises(lifecycle.LifecycleError, match="nothing to rotate"):
        lifecycle.rotate_audit(app)
    app.ctx.audit.append("test.one", value=1)
    app.ctx.audit.append("test.two", value=2)
    path = app.ctx.audit.path
    path.write_text(path.read_text(encoding="utf-8").replace('"value":1', '"value":9'), encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="does not verify"):
        lifecycle.rotate_audit(app)
    assert list((app.ctx.data_dir / "audit-archive").glob("*")) == []
    assert "test.two" in path.read_text(encoding="utf-8")


def test_archives_that_nothing_links_are_warnings(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.audit.append("test.event")
    report = lifecycle.rotate_audit(app)
    stray = report.archive.parent / "audit-20260101T000000Z-0123456789abcdef.jsonl"
    stray.write_text("", encoding="utf-8")

    chain = lifecycle.verify_audit_chain(app.ctx.audit.path, report.archive.parent)

    assert chain.ok
    assert chain.warnings == (f"{stray.name} is not linked from the current chain",)
