"""Opt-in retention for original statement files, enabled by ``[retention.connectors] finance = <days>``.

The import inbox moves imported statements to ``processed/`` and unreadable ones to ``failed/``. Those
originals can contain full account numbers even though the ledger masks them. A file is removed only when both
its modification time and its status-change time are older than the cutoff, so a statement downloaded long ago
and imported recently is kept for the full period on file systems that record the move. Only regular files
with a statement suffix directly inside those two folders are considered; links, hidden files, other names,
and every ledger table are left alone. File removal is per file, not one transaction: a file that cannot be
removed is counted in ``files_not_removed`` and the rest continue.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

from switchboard.connectors.finance.inbox import FAILED, PROCESSED, SUPPORTED_SUFFIXES, inbox_problems
from switchboard.connectors.finance.models import NAME
from switchboard.connectors.finance.settings import load_settings
from switchboard.context import AppContext
from switchboard.errors import ToolError

EXPORT_TABLES = ("finance_accounts", "finance_imports", "finance_transactions", "finance_budget_alerts")


def purge(ctx: AppContext, *, older_than: datetime, dry_run: bool) -> dict[str, int]:
    counts = {"processed_files": 0, "failed_files": 0, "files_not_removed": 0}
    import_dir = load_settings(ctx.connector_config(NAME)).import_dir
    if import_dir is None:
        return counts
    problems = inbox_problems(import_dir)
    if problems:
        raise ToolError(problems[0])
    limit = older_than.timestamp()
    for folder, key in ((PROCESSED, "processed_files"), (FAILED, "failed_files")):
        removed, kept = _purge_folder(import_dir / folder, limit, dry_run=dry_run)
        counts[key] = removed
        counts["files_not_removed"] += kept
    return counts


def _purge_folder(folder: Path, limit: float, *, dry_run: bool) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(folder, flags | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return 0, 0
    except OSError as exc:
        raise ToolError(f"{folder.name}/ in import_dir cannot be opened safely: {exc.strerror}") from exc
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ToolError(f"{folder.name}/ in import_dir must be your own folder, not writable by others")
        removed = failures = 0
        for name in sorted(os.listdir(directory)):  # noqa: PTH208 - pathlib cannot list a descriptor
            if name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            try:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(entry.st_mode) or max(entry.st_mtime, entry.st_ctime) >= limit:
                continue
            if not dry_run:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    continue
                except OSError:
                    failures += 1
                    continue
            removed += 1
        return removed, failures
    finally:
        os.close(directory)
