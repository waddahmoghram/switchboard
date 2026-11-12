"""Command-line wiring for backups, data export and retention, and audit-log archives.

``backup verify`` and ``backup restore`` never build the application: building it would create a fresh
database in the data directory a restore needs empty. See ``docs/DATA_LIFECYCLE.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from switchboard import lifecycle
from switchboard.config import load_config

if TYPE_CHECKING:
    from switchboard.app import App


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``backup {create,verify,restore}``, ``data {export,purge}``, and ``audit-archive``."""
    backup = subparsers.add_parser("backup", help="create, verify, or restore a consistent backup")
    backup_actions = backup.add_subparsers(dest="backup_command", metavar="<action>", required=True)
    create = backup_actions.add_parser(
        "create", help="copy the database, audit log, and audit archives into a new private directory"
    )
    create.add_argument("destination", type=Path, help="directory to create; it must not exist yet")
    create.set_defaults(app_handler=cmd_backup_create)
    verify = backup_actions.add_parser(
        "verify", help="check hashes, database integrity, schema versions, and the audit chain read-only"
    )
    verify.add_argument("source", type=Path)
    verify.add_argument(
        "--expect-audit-head",
        metavar="HASH",
        help="an audit head recorded elsewhere when the backup was made",
    )
    verify.set_defaults(handler=cmd_backup_verify)
    restore = backup_actions.add_parser(
        "restore", help="verify, then restore into a new or empty data directory with the kill switch on"
    )
    restore.add_argument("source", type=Path)
    restore.add_argument(
        "--target", type=Path, help="new or empty data directory (default: the configured data_dir)"
    )
    restore.set_defaults(handler=cmd_backup_restore)

    data = subparsers.add_parser("data", help="export or purge stored personal data")
    data_actions = data.add_subparsers(dest="data_command", metavar="<action>", required=True)
    export = data_actions.add_parser("export", help="write a private JSON export (mode 600)")
    export.add_argument("destination", type=Path, help="file to create; it must not exist yet")
    export.set_defaults(app_handler=cmd_data_export)
    purge = data_actions.add_parser(
        "purge", help="apply [retention]; open journal entries and live approvals are never purged"
    )
    purge.add_argument("--dry-run", action="store_true", help="count what would be removed; remove nothing")
    purge.set_defaults(app_handler=cmd_data_purge)

    archive = subparsers.add_parser(
        "audit-archive", help="rotate the audit log into a linked private archive, or verify the linked chain"
    )
    archive_actions = archive.add_subparsers(dest="audit_archive_command", metavar="<action>", required=True)
    archive_actions.add_parser(
        "rotate", help="archive the current log; the new log starts with a record linking the archive"
    ).set_defaults(app_handler=cmd_audit_archive_rotate)
    archive_actions.add_parser(
        "verify", help="verify the live log and follow every linked archive"
    ).set_defaults(app_handler=cmd_audit_archive_verify)


def cmd_backup_create(args: argparse.Namespace, app: App) -> int:
    report = lifecycle.create_backup(app, args.destination)
    audit = report.manifest["audit"]
    print(f"backup written to {report.path}")
    print(f"audit head {audit['head']} ({audit['records']} records)")
    print(f"manifest sha256 {report.manifest_sha256}")
    print("keep the audit head and manifest hash somewhere else to detect a rewritten backup later")
    for problem in report.problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 1 if report.problems else 0


def cmd_backup_verify(args: argparse.Namespace) -> int:
    result = lifecycle.verify_backup(args.source, expect_audit_head=args.expect_audit_head)
    for warning in result.warnings:
        print(f"warning: {warning}")
    for problem in result.problems:
        print(f"problem: {problem}", file=sys.stderr)
    if not result.ok:
        print(f"BROKEN: {args.source} does not verify", file=sys.stderr)
        return 1
    versions = ", ".join(f"{name} {version}" for name, version in sorted(result.schema_versions.items()))
    print(f"ok: {args.source}")
    print(f"schema versions: {versions}")
    print(f"audit head {result.audit_head} ({result.audit_records} records)")
    print("this shows the copy is consistent, not that providers or devices still match it")
    return 0


def cmd_backup_restore(args: argparse.Namespace) -> int:
    target = args.target if args.target is not None else load_config(getattr(args, "config", None)).data_dir
    report = lifecycle.restore_backup(args.source, target)
    print(f"restored {len(report.files)} file(s) into {report.target}")
    print(f"audit head before the restore record: {report.audit_head}")
    print("the kill switch is ON there: review approvals and uncertain actions, then turn it off")
    print("configuration, secrets, and OAuth token files are not in backups; restore them separately")
    return 0


def cmd_data_export(args: argparse.Namespace, app: App) -> int:
    report = lifecycle.export_data(app, args.destination)
    summary = ", ".join(f"{name} {count}" for name, count in report.counts.items())
    print(f"wrote {report.path} (mode 600): {summary}")
    for name, tables in report.connectors.items():
        print(f"{name}: " + ", ".join(f"{table} {count}" for table, count in tables.items()))
    for problem in report.problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 1 if report.problems else 0


def cmd_data_purge(args: argparse.Namespace, app: App) -> int:
    report = lifecycle.purge_data(app, dry_run=args.dry_run)
    verb = "would remove" if report.dry_run else "removed"
    if report.dry_run:
        print("dry run: nothing was removed")
    for name, count in report.counts.items():
        print(f"{name}: {verb} {count} older than {report.cutoffs[name]}")
    for name, counts in report.connectors.items():
        detail = ", ".join(f"{key} {value}" for key, value in counts.items()) or "nothing"
        print(f"{name}: {verb} {detail} older than {report.cutoffs[f'connector:{name}']}")
    for name, reason in report.skipped.items():
        print(f"{name}: skipped ({reason})")
    for problem in report.problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 1 if report.problems else 0


def cmd_audit_archive_rotate(args: argparse.Namespace, app: App) -> int:
    report = lifecycle.rotate_audit(app)
    print(f"archived {report.records} record(s) to {report.archive}")
    print(f"archive sha256 {report.archive_sha256}; previous head {report.previous_head}")
    print(f"new head {report.head}")
    return 0


def cmd_audit_archive_verify(args: argparse.Namespace, app: App) -> int:
    report = lifecycle.verify_audit_chain(app.ctx.audit.path, app.ctx.data_dir / lifecycle.ARCHIVE_DIR)
    for warning in report.warnings:
        print(f"warning: {warning}")
    if not report.ok:
        for problem in report.problems:
            print(f"BROKEN: {problem}", file=sys.stderr)
        return 1
    print(f"ok: {len(report.segments)} segment(s), {report.records} records")
    for segment in report.segments:
        print(f"{segment.name}: {segment.records} records, head {segment.head}")
    return 0
