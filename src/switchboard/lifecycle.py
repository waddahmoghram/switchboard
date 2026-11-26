"""Data lifecycle: consistent backups, verification and restore, exports, retention, and audit archives.

A backup copies the SQLite database through its online backup interface, and the audit log with its archived
segments, while holding the audit log's lock: no record is half-copied and the recorded head matches the
copy. Configuration, secret files, OAuth token files, and connector import folders are not part of a backup.
Verification opens the copy read-only and shows only that the copy is internally consistent and matches its
manifest. It does not show that providers or devices are still in the state the backup describes, and a
person able to rewrite the whole backup can recompute every hash in it; compare the audit head with a value
recorded elsewhere to detect that.

Retention never deletes open journal entries (``in_flight`` or ``uncertain``), pending or executing approvals,
running budget reservations, or unprocessed events. Connector data is purged only for enabled connectors that
have an explicit ``[retention.connectors]`` entry and provide ``switchboard.connectors.<name>.retention``
with ``purge(ctx, *, older_than, dry_run) -> dict[str, int]``. That module may also name the connector's own
tables in ``EXPORT_TABLES`` for ``data export``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import IO, Any, TypeGuard
from urllib.parse import quote

from switchboard import __version__
from switchboard.actions import ActionJournal
from switchboard.app import KNOWN_CONNECTORS, App
from switchboard.audit import GENESIS, AuditLog
from switchboard.config import ensure_private_dir
from switchboard.db import CORE_MIGRATIONS, Database
from switchboard.errors import SwitchboardError
from switchboard.locking import fcntl
from switchboard.util import canonical_json, iso, json_equal, json_loads, now, sha256_hex

log = logging.getLogger(__name__)

BACKUP_FORMAT = "switchboard-backup"
EXPORT_FORMAT = "switchboard-export"
FORMAT_VERSION = 1
DATABASE_FILE = "switchboard.sqlite3"
AUDIT_FILE = "audit.jsonl"
ARCHIVE_DIR = "audit-archive"
MANIFEST_FILE = "manifest.json"
RUNNER_LOCK = "runner.lock"
KILL_SWITCH = "KILL_SWITCH"
ROTATION_KIND = "audit.rotated"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LINKED_ARCHIVES = 10_000
_CHUNK = 1024 * 1024
_ARCHIVE_NAME = re.compile(r"audit-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.jsonl")
_HASH = re.compile(r"[0-9a-f]{64}")
_ANCHOR = re.compile(r"([1-9][0-9]{0,17}):([0-9a-f]{64})")
_TABLE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")

Clock = Callable[[], datetime]


class LifecycleError(SwitchboardError):
    """A backup, restore, export, purge, or rotation was refused or could not finish safely."""


# ---- files and locks --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDigest:
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "bytes": self.size}


def _chunks(fd: int) -> Iterator[bytes]:
    offset = 0
    while True:
        chunk = os.pread(fd, _CHUNK, offset)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


def _open_regular(path: Path) -> int:
    """Open a regular file for reading without following a final symbolic link."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LifecycleError(f"{path} is not a regular file")
    return fd


def _digest_file(path: Path) -> FileDigest:
    fd = _open_regular(path)
    try:
        digest = hashlib.sha256()
        size = 0
        for chunk in _chunks(fd):
            digest.update(chunk)
            size += len(chunk)
        return FileDigest(digest.hexdigest(), size)
    finally:
        os.close(fd)


def _create_private_file(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)


def _write_all(fd: int, data: bytes, offset: int | None = None) -> None:
    view = memoryview(data)
    position = offset
    while view:
        if position is None:
            written = os.write(fd, view)
        else:
            written = os.pwrite(fd, view, position)
            position += written
        view = view[written:]


def _copy_fd(source_fd: int, destination: Path) -> FileDigest:
    """Copy a whole open file into a new private file, sync it, and return the copied bytes' digest."""
    fd = _create_private_file(destination)
    try:
        digest = hashlib.sha256()
        size = 0
        for chunk in _chunks(source_fd):
            _write_all(fd, chunk)
            digest.update(chunk)
            size += len(chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
    return FileDigest(digest.hexdigest(), size)


def _copy_file(source: Path, destination: Path) -> FileDigest:
    fd = _open_regular(source)
    try:
        return _copy_fd(fd, destination)
    finally:
        os.close(fd)


def _write_new_file(path: Path, body: bytes) -> None:
    fd = _create_private_file(path)
    try:
        _write_all(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_file(path: Path) -> None:
    fd = _open_regular(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    # Some platforms and file systems cannot sync a directory; the files themselves are already synced.
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_created(paths: list[Path]) -> None:
    """Best-effort removal of what a failed operation created, newest first."""
    for path in reversed(paths):
        with contextlib.suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                for suffix in _SQLITE_SIDE_FILES:
                    (path / f"{DATABASE_FILE}{suffix}").unlink(missing_ok=True)
                path.rmdir()
            else:
                path.unlink(missing_ok=True)


def _whole(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@contextmanager
def _locked_audit(path: Path, *, exclusive: bool) -> Iterator[int | None]:
    """Hold the audit log's lock: shared pauses appends, exclusive also excludes other readers that lock.

    Yields the open descriptor, or None when there is no audit log yet.
    """
    flags = (os.O_RDWR if exclusive else os.O_RDONLY) | _NOFOLLOW | _CLOEXEC
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        fd = -1
    if fd < 0:
        yield None
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LifecycleError(f"{path} is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield fd
    finally:
        os.close(fd)  # closing the descriptor releases the lock


@contextmanager
def hold_runner_lock(data_dir: Path) -> Iterator[bool]:
    """Hold ``runner.lock``, the lock ``switchboard run`` takes; yields False while another process has it."""
    fd = os.open(data_dir / RUNNER_LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextmanager
def purge_transaction(conn: sqlite3.Connection, *, dry_run: bool) -> Iterator[sqlite3.Connection]:
    """An immediate write transaction that commits, or rolls back when ``dry_run`` is set.

    A dry run executes the very statements an apply would and reports their row counts, then keeps nothing.
    Retention hooks use this so a dry run and an apply at the same moment report the same numbers.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")


# ---- schema versions --------------------------------------------------------------------------------


def known_schema_versions() -> dict[str, int]:
    """The newest schema version this Switchboard understands for the core and each known connector."""
    versions = {"core": max(version for version, _statements in CORE_MIGRATIONS)}
    for name, module_path in KNOWN_CONNECTORS.items():
        migrations = getattr(importlib.import_module(module_path), "MIGRATIONS", None) or []
        if migrations:
            versions[name] = max(int(version) for version, _statements in migrations)
    return versions


def _schema_versions(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT component, MAX(version) FROM schema_migrations GROUP BY component ORDER BY component"
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(os.fspath(path))}?mode=ro", uri=True)


# ---- backups ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupReport:
    path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    problems: tuple[str, ...]


def _new_private_directory(path: Path) -> Path:
    try:
        parent = os.stat(path.parent)
    except FileNotFoundError:
        raise LifecycleError(f"{path.parent} does not exist") from None
    if not stat.S_ISDIR(parent.st_mode):
        raise LifecycleError(f"{path.parent} is not a directory")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        raise LifecycleError(f"{path} already exists; backups always go into a new directory") from None
    os.chmod(path, 0o700)
    return path


def _backup_database(db: Database, destination: Path) -> FileDigest:
    """Online copy through SQLite's backup interface, left in rollback-journal mode for read-only checks."""
    os.close(_create_private_file(destination))
    with closing(db.connect()) as source, closing(sqlite3.connect(destination, isolation_level=None)) as copy:
        source.backup(copy)
        mode = copy.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise LifecycleError("the database copy could not leave write-ahead logging mode")
    _sync_file(destination)
    return _digest_file(destination)


def _copy_archives(source: Path, target: Path, created: list[Path]) -> dict[str, FileDigest]:
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LifecycleError(f"{source} must be a real directory")
    copied: dict[str, FileDigest] = {}
    for name in sorted(os.listdir(source)):
        if not _ARCHIVE_NAME.fullmatch(name):
            log.warning("not backing up %s: it is not named like an audit archive", source / name)
            continue
        if not copied:
            os.mkdir(target, 0o700)
            created.append(target)
        created.append(target / name)
        copied[name] = _copy_file(source / name, target / name)
    if copied:
        _sync_directory(target)
    return copied


def create_backup(app: App, destination: Path, *, clock: Clock = now) -> BackupReport:
    """Copy the database, audit log, and audit archives into a new private directory with a manifest.

    Audit appends pause while the copy is made. A database change whose audit record had not been written
    yet can appear in the copy without that record.
    """
    root = _new_private_directory(destination)
    created: list[Path] = [root]
    try:
        files: dict[str, FileDigest] = {}
        with _locked_audit(app.ctx.audit.path, exclusive=False) as audit_fd:
            created.append(root / DATABASE_FILE)
            files[DATABASE_FILE] = _backup_database(app.ctx.db, root / DATABASE_FILE)
            created.append(root / AUDIT_FILE)
            if audit_fd is None:
                _write_new_file(root / AUDIT_FILE, b"")
                files[AUDIT_FILE] = _digest_file(root / AUDIT_FILE)
            else:
                files[AUDIT_FILE] = _copy_fd(audit_fd, root / AUDIT_FILE)
            archives = _copy_archives(app.ctx.data_dir / ARCHIVE_DIR, root / ARCHIVE_DIR, created)
            files.update({f"{ARCHIVE_DIR}/{name}": digest for name, digest in archives.items()})
        with closing(_read_only(root / DATABASE_FILE)) as conn:
            schema = _schema_versions(conn)
        audit = AuditLog(root / AUDIT_FILE).verify()
        chain = verify_audit_chain(root / AUDIT_FILE, root / ARCHIVE_DIR)
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "format_version": FORMAT_VERSION,
            "engine": "python",
            "switchboard_version": __version__,
            "created_at": iso(clock()),
            "schema_versions": schema,
            "audit": {"head": audit.head, "records": audit.records},
            "kill_switch": app.policy.kill_switch.exists(),
            "files": {name: digest.to_dict() for name, digest in sorted(files.items())},
        }
        body = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        created.append(root / MANIFEST_FILE)
        _write_new_file(root / MANIFEST_FILE, body)
        _sync_directory(root)
    except BaseException:
        _remove_created(created)
        raise
    manifest_sha256 = sha256_hex(body)
    app.ctx.audit.append(
        "backup.created", destination=str(root), audit_head=audit.head, manifest_sha256=manifest_sha256
    )
    return BackupReport(root, manifest, manifest_sha256, chain.problems)


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: dict[str, Any] | None
    files: dict[str, FileDigest]
    schema_versions: dict[str, int]
    audit_head: str | None
    audit_records: int | None

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems


def _read_manifest(path: Path, problems: list[str]) -> dict[str, Any] | None:
    try:
        fd = _open_regular(path)
    except FileNotFoundError:
        problems.append(f"{MANIFEST_FILE} is missing")
        return None
    except (OSError, LifecycleError) as exc:
        problems.append(f"{MANIFEST_FILE} cannot be read safely: {exc}")
        return None
    try:
        if os.fstat(fd).st_size > MAX_MANIFEST_BYTES:
            problems.append(f"{MANIFEST_FILE} is larger than {MAX_MANIFEST_BYTES} bytes")
            return None
        raw = b"".join(_chunks(fd))
    finally:
        os.close(fd)
    try:
        manifest = json_loads(raw)
    except (ValueError, RecursionError):
        problems.append(f"{MANIFEST_FILE} is not valid JSON")
        return None
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        problems.append(f"{MANIFEST_FILE} does not describe a Switchboard backup")
        return None
    version = manifest.get("format_version")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        problems.append(f"backup format version {version!r} is not supported")
        return None
    return manifest


def _backup_member(name: str) -> bool:
    if name in (DATABASE_FILE, AUDIT_FILE):
        return True
    prefix = f"{ARCHIVE_DIR}/"
    return name.startswith(prefix) and _ARCHIVE_NAME.fullmatch(name[len(prefix) :]) is not None


def _manifest_files(manifest: Mapping[str, Any], problems: list[str]) -> dict[str, FileDigest]:
    table = manifest.get("files")
    if not isinstance(table, dict):
        problems.append(f"{MANIFEST_FILE} has no file table")
        return {}
    files: dict[str, FileDigest] = {}
    for name, entry in sorted(table.items()):
        if not isinstance(name, str) or not _backup_member(name):
            problems.append(f"{MANIFEST_FILE} lists an unexpected file {name!r}")
            continue
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        size = entry.get("bytes") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or not _whole(size):
            problems.append(f"{MANIFEST_FILE} has a malformed entry for {name}")
            continue
        files[name] = FileDigest(digest, size)
    for required in (DATABASE_FILE, AUDIT_FILE):
        if required not in table:
            problems.append(f"{MANIFEST_FILE} does not list {required}")
    return files


def _backup_entries(root: Path, problems: list[str]) -> set[str]:
    """Regular files that may belong to a backup; anything else in the directory is a problem."""
    present: set[str] = set()
    for name in sorted(os.listdir(root)):
        info = os.lstat(root / name)
        if name == MANIFEST_FILE:
            continue
        if name == ARCHIVE_DIR and stat.S_ISDIR(info.st_mode):
            for member in sorted(os.listdir(root / name)):
                relative = f"{ARCHIVE_DIR}/{member}"
                if stat.S_ISREG(os.lstat(root / relative).st_mode) and _backup_member(relative):
                    present.add(relative)
                else:
                    problems.append(f"unexpected entry {relative}")
            continue
        if stat.S_ISREG(info.st_mode) and name in (DATABASE_FILE, AUDIT_FILE):
            present.add(name)
            continue
        problems.append(f"unexpected entry {name}")
    return present


def _check_database_copy(path: Path, recorded: Any, problems: list[str]) -> dict[str, int]:
    try:
        with closing(_read_only(path)) as conn:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                problems.append("database integrity check failed: " + "; ".join(integrity[:5]))
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                problems.append(f"the database has {len(violations)} foreign key violation(s)")
            schema = _schema_versions(conn)
    except sqlite3.Error as exc:
        problems.append(f"the database copy cannot be read: {exc}")
        return {}
    if not json_equal(recorded, schema):
        problems.append("the database's schema versions differ from the manifest")
    if "core" not in schema:
        problems.append("the database has no core schema")
    known = known_schema_versions()
    for component, version in sorted(schema.items()):
        newest = known.get(component)
        if newest is None:
            problems.append(f"schema component {component!r} is not recognized by this Switchboard")
        elif version > newest:
            problems.append(
                f"{component} schema version {version} is newer than this Switchboard understands ({newest})"
            )
    return schema


def verify_backup(path: Path, *, expect_audit_head: str | None = None) -> BackupVerification:
    """Check a backup without changing it: manifest, file hashes, database, schema, and audit chain."""
    problems: list[str] = []
    warnings: list[str] = []
    files: dict[str, FileDigest] = {}
    schema: dict[str, int] = {}
    audit_head: str | None = None
    audit_records: int | None = None
    manifest: dict[str, Any] | None = None

    def result() -> BackupVerification:
        return BackupVerification(
            path, tuple(problems), tuple(warnings), manifest, files, schema, audit_head, audit_records
        )

    if expect_audit_head is not None and not _HASH.fullmatch(expect_audit_head):
        problems.append("the expected audit head must be 64 lowercase hexadecimal characters")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        problems.append(f"{path} does not exist")
        return result()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        problems.append(f"{path} must be a real directory, not a link or file")
        return result()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        warnings.append(f"{path} belongs to another account")
    if info.st_mode & 0o077:
        warnings.append(f"{path} is accessible to other accounts; run: chmod 700 {path}")
    manifest = _read_manifest(path / MANIFEST_FILE, problems)
    if manifest is None:
        return result()
    files = _manifest_files(manifest, problems)
    try:
        present = _backup_entries(path, problems)
    except OSError as exc:
        problems.append(f"cannot list the backup: {exc.strerror or exc}")
        return result()
    listed = set(files) | {name for name in (DATABASE_FILE, AUDIT_FILE) if name not in files}
    problems.extend(f"{name} is missing" for name in sorted(listed - present))
    problems.extend(f"{name} is not listed in the manifest" for name in sorted(present - listed))
    for name, expected in sorted(files.items()):
        if name not in present:
            continue
        try:
            actual = _digest_file(path / name)
        except (OSError, LifecycleError) as exc:
            problems.append(f"{name} cannot be read: {exc}")
            continue
        if actual != expected:
            problems.append(f"{name} does not match the hash in the manifest")
    if DATABASE_FILE in present:
        schema = _check_database_copy(path / DATABASE_FILE, manifest.get("schema_versions"), problems)
    if AUDIT_FILE in present:
        audit = AuditLog(path / AUDIT_FILE).verify()
        audit_head, audit_records = audit.head, audit.records
        recorded = manifest.get("audit")
        recorded_head = recorded.get("head") if isinstance(recorded, dict) else None
        recorded_records = recorded.get("records") if isinstance(recorded, dict) else None
        if recorded_head != audit.head or not json_equal(recorded_records, audit.records):
            problems.append("the audit log's head or record count differs from the manifest")
        if expect_audit_head is not None and expect_audit_head != audit.head:
            problems.append("the audit head differs from the independently recorded head")
        chain = verify_audit_chain(path / AUDIT_FILE, path / ARCHIVE_DIR)
        problems.extend(chain.problems)
        warnings.extend(chain.warnings)
    return result()


@dataclass(frozen=True)
class RestoreReport:
    target: Path
    audit_head: str
    schema_versions: dict[str, int]
    files: tuple[str, ...]


def _restore_directory(path: Path) -> Path:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _new_private_directory(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LifecycleError(f"{path} must be a real directory, not a link or file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LifecycleError(f"{path} belongs to another account")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _leftovers(directory: Path) -> list[str]:
    return sorted(set(os.listdir(directory)) - {RUNNER_LOCK})


def restore_backup(source: Path, target: Path) -> RestoreReport:
    """Verify ``source``, then copy it into a new or empty private data directory with the kill switch on.

    Refused while a runner holds that directory's ``runner.lock``. The restored directory starts with the
    kill switch on, so nothing acts until the operator has reviewed pending approvals and uncertain actions.
    """
    verification = verify_backup(source)
    if not verification.ok or verification.audit_head is None:
        details = "; ".join(verification.problems) or "the manifest is unusable"
        raise LifecycleError(f"the backup does not verify, so nothing was restored: {details}")
    directory = _restore_directory(target)
    if _leftovers(directory):
        raise LifecycleError(f"{directory} is not empty; restore only into a new or empty directory")
    with hold_runner_lock(directory) as acquired:
        if not acquired:
            raise LifecycleError(f"a runner holds {directory / RUNNER_LOCK}; stop it before restoring")
        leftovers = _leftovers(directory)
        if leftovers:
            raise LifecycleError(f"{directory} is not empty ({', '.join(leftovers[:5])})")
        created: list[Path] = []
        try:
            for name, expected in sorted(verification.files.items()):
                destination = directory / name
                if destination.parent != directory and not destination.parent.exists():
                    os.mkdir(destination.parent, 0o700)
                    created.append(destination.parent)
                created.append(destination)
                if _copy_file(source / name, destination) != expected:
                    raise LifecycleError(f"{name} changed after it was verified; nothing was restored")
            created.append(directory / KILL_SWITCH)
            os.close(_create_private_file(directory / KILL_SWITCH))
            if (directory / ARCHIVE_DIR).is_dir():
                _sync_directory(directory / ARCHIVE_DIR)
            _sync_directory(directory)
        except BaseException:
            _remove_created(created)
            raise
    manifest = verification.manifest or {}
    AuditLog(directory / AUDIT_FILE).append(
        "backup.restored",
        source=str(source),
        backup_created_at=manifest.get("created_at"),
        backup_audit_head=verification.audit_head,
        kill_switch=True,
    )
    return RestoreReport(
        directory, verification.audit_head, verification.schema_versions, tuple(sorted(verification.files))
    )


# ---- export -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportReport:
    path: Path
    counts: dict[str, int]
    connectors: dict[str, dict[str, int]]
    problems: tuple[str, ...]


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    return str(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=_json_default)


class _JsonObjectWriter:
    """Write one JSON object incrementally, so large tables are never held in memory."""

    def __init__(self, handle: IO[str]) -> None:
        self._handle = handle
        self._first = True
        handle.write("{")

    def _key(self, name: str) -> None:
        self._handle.write("\n  " if self._first else ",\n  ")
        self._first = False
        self._handle.write(json.dumps(name) + ": ")

    def field(self, name: str, value: Any) -> None:
        self._key(name)
        self._handle.write(_dumps(value))

    def _items(self, items: Iterable[Any], indent: str) -> int:
        self._handle.write("[")
        count = 0
        for item in items:
            self._handle.write(("\n" if count == 0 else ",\n") + indent + "  " + _dumps(item))
            count += 1
        self._handle.write(("\n" + indent + "]") if count else "]")
        return count

    def array(self, name: str, items: Iterable[Any]) -> int:
        self._key(name)
        return self._items(items, "  ")

    def groups(
        self, name: str, groups: Mapping[str, Mapping[str, Callable[[], Iterable[Any]]]]
    ) -> dict[str, dict[str, int]]:
        """``{"group": {"array": [...]}}``, with each array produced lazily."""
        self._key(name)
        counts: dict[str, dict[str, int]] = {}
        self._handle.write("{")
        for group_index, (group, arrays) in enumerate(groups.items()):
            self._handle.write(("\n" if group_index == 0 else ",\n") + "    " + json.dumps(group) + ": {")
            counts[group] = {}
            for array_index, (array, rows) in enumerate(arrays.items()):
                self._handle.write(
                    ("\n" if array_index == 0 else ",\n") + "      " + json.dumps(array) + ": "
                )
                counts[group][array] = self._items(rows(), "      ")
            self._handle.write("\n    }" if arrays else "}")
        self._handle.write("\n  }" if groups else "}")
        return counts

    def close(self) -> None:
        self._handle.write("\n}\n")


def _parsed(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json_loads(text)
    except (ValueError, RecursionError):
        return {"unparsed": text}


def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["args"] = _parsed(item.pop("args_json"))
    result = item.pop("result_json")
    item["result"] = None if result is None else _parsed(result)
    return item


def _action_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("owner", None)
    item["args"] = _parsed(item.pop("args_json"))
    return item


def _plain_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = _parsed(item.pop("payload_json"))
    return item


def retention_hook(name: str) -> ModuleType | None:
    """``switchboard.connectors.<name>.retention`` when that connector provides one."""
    package = KNOWN_CONNECTORS.get(name)
    if package is None:
        raise LifecycleError(f"{name!r} is not a known connector")
    module_name = f"{package}.retention"
    if importlib.util.find_spec(module_name) is None:
        return None
    module = importlib.import_module(module_name)
    if not callable(getattr(module, "purge", None)):
        raise LifecycleError(f"{module_name} must define purge(ctx, *, older_than, dry_run)")
    return module


def export_tables(name: str, module: ModuleType) -> tuple[str, ...]:
    """The connector's own tables a retention module offers for export."""
    tables = getattr(module, "EXPORT_TABLES", ())
    if not isinstance(tables, tuple) or not all(
        isinstance(table, str) and _TABLE.fullmatch(table) and table.startswith(f"{name}_")
        for table in tables
    ):
        raise LifecycleError(f"{name}: EXPORT_TABLES must name the connector's own tables")
    return tables


def _table_rows(conn: sqlite3.Connection, table: str) -> Callable[[], Iterable[dict[str, Any]]]:
    def rows() -> Iterable[dict[str, Any]]:
        # The name matched the connector's prefix and sqlite_master; values are never interpolated.
        query = f'SELECT * FROM "{table}" ORDER BY rowid'  # noqa: S608
        return (dict(row) for row in conn.execute(query))

    return rows


def _connector_exports(
    app: App, conn: sqlite3.Connection, problems: list[str]
) -> dict[str, dict[str, Callable[[], Iterable[dict[str, Any]]]]]:
    existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    exports: dict[str, dict[str, Callable[[], Iterable[dict[str, Any]]]]] = {}
    for name in sorted(app.connectors):
        try:
            module = retention_hook(name)
            tables = () if module is None else export_tables(name, module)
        except Exception as exc:
            problems.append(f"{name}: its retention module cannot be used ({type(exc).__name__}: {exc})")
            continue
        selected = {table: _table_rows(conn, table) for table in tables if table in existing}
        if selected:
            exports[name] = selected
    return exports


def export_data(app: App, destination: Path, *, clock: Clock = now) -> ExportReport:
    """Write a private JSON export (mode 600) from one consistent database snapshot.

    It contains approvals and the action journal with their arguments, events with payloads, tool-call budget
    rows, a key-value summary without values, and tables that enabled connectors offer for export.
    """
    try:
        fd = _create_private_file(destination)
    except FileExistsError:
        raise LifecycleError(f"{destination} already exists; an export never overwrites a file") from None
    except OSError as exc:
        raise LifecycleError(f"cannot create {destination}: {exc.strerror or exc}") from exc
    counts: dict[str, int] = {}
    connectors: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            stream = _JsonObjectWriter(handle)
            with closing(app.ctx.db.connect()) as conn:
                conn.execute("BEGIN")  # one read snapshot for every table
                try:
                    counts, connectors = _write_export(stream, app, conn, clock, problems)
                finally:
                    conn.execute("ROLLBACK")
            stream.close()
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    app.ctx.audit.append("data.exported", destination=str(destination), counts=counts, connectors=connectors)
    return ExportReport(destination, counts, connectors, tuple(problems))


_EXPORT_QUERIES: dict[str, tuple[str, Callable[[sqlite3.Row], dict[str, Any]]]] = {
    "approvals": ("SELECT * FROM approvals ORDER BY created_at, id", _approval_row),
    "actions": ("SELECT * FROM action_journal ORDER BY started_at, id", _action_row),
    "events": ("SELECT * FROM events ORDER BY id", _event_row),
    "tool_calls": ("SELECT * FROM tool_calls ORDER BY id", _plain_row),
    "kv_namespaces": (
        "SELECT namespace, COUNT(*) AS keys, SUM(LENGTH(value)) AS characters, "
        "MAX(updated_at) AS last_updated FROM kv GROUP BY namespace ORDER BY namespace",
        _plain_row,
    ),
}


def _write_export(
    stream: _JsonObjectWriter, app: App, conn: sqlite3.Connection, clock: Clock, problems: list[str]
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    stream.field("format", EXPORT_FORMAT)
    stream.field("format_version", FORMAT_VERSION)
    stream.field("engine", "python")
    stream.field("switchboard_version", __version__)
    stream.field("created_at", iso(clock()))
    stream.field("schema_versions", _schema_versions(conn))
    stream.field("audit_head", app.ctx.audit.head())
    counts: dict[str, int] = {}
    for section, (query, convert) in _EXPORT_QUERIES.items():
        counts[section] = stream.array(section, (convert(row) for row in conn.execute(query)))
    connectors = stream.groups("connectors", _connector_exports(app, conn, problems))
    stream.field("problems", problems)
    return counts, connectors


# ---- retention --------------------------------------------------------------------------------------

_PURGE_TOOL_CALLS = "DELETE FROM tool_calls WHERE created_at < ? AND outcome != 'running'"
# A pending approval that expired before the cutoff can never be approved any more, like an expired one.
_PURGE_APPROVALS = (
    "DELETE FROM approvals WHERE COALESCE(decided_at, expires_at) < ? AND ("
    "status IN ('denied', 'expired', 'executed', 'failed') OR (status = 'pending' AND expires_at < ?))"
)
# ActionJournal.purge_terminal and EventBus.prune cannot rehearse, so a dry run repeats their statements.
_REHEARSE_ACTIONS = (
    "DELETE FROM action_journal WHERE state IN "
    "('succeeded', 'failed', 'reconciled_applied', 'reconciled_not_applied') "
    "AND COALESCE(resolved_at, finished_at, started_at) < ?"
)
_REHEARSE_EVENTS = (
    "DELETE FROM events WHERE processed_at IS NOT NULL AND created_at < ? AND dedupe_key IS NULL",
    "UPDATE events SET payload_json = '{}' WHERE processed_at IS NOT NULL AND created_at < ? "
    "AND dedupe_key IS NOT NULL AND payload_json != '{}'",
)


@dataclass(frozen=True)
class PurgeReport:
    dry_run: bool
    cutoffs: dict[str, str]
    counts: dict[str, int]
    connectors: dict[str, dict[str, int]]
    skipped: dict[str, str]
    problems: tuple[str, ...]


def _checked_counts(name: str, result: Any) -> dict[str, int]:
    if not isinstance(result, dict) or not all(
        isinstance(key, str) and _whole(value) for key, value in result.items()
    ):
        raise LifecycleError(f"{name}: the retention hook must return counts as whole numbers")
    return dict(result)


def purge_data(app: App, *, dry_run: bool, clock: Clock = now) -> PurgeReport:
    """Apply ``[retention]``; ``dry_run`` counts the same rows and keeps them.

    Decided or long-expired approvals, finished or reconciled journal entries, finished budget rows, processed
    events, and opted-in connector data older than their periods are removed. Open journal entries, approvals
    that can still be approved or are executing, and running budget rows never are.
    """
    config = app.ctx.config
    moment = clock()
    days = {
        "tool_calls": config.bounded("retention", "tool_calls_days"),
        "approvals": config.bounded("retention", "approvals_days"),
        "actions": config.bounded("retention", "actions_days"),
        "events": config.bounded("switchboard", "event_retention_days"),
    }
    cutoffs = {name: iso(moment - timedelta(days=value)) for name, value in days.items()}
    counts: dict[str, int] = {}
    with app.ctx.db.session() as conn:
        with purge_transaction(conn, dry_run=dry_run):
            counts["tool_calls"] = conn.execute(_PURGE_TOOL_CALLS, (cutoffs["tool_calls"],)).rowcount
        with purge_transaction(conn, dry_run=dry_run):
            counts["approvals"] = conn.execute(
                _PURGE_APPROVALS, (cutoffs["approvals"], cutoffs["approvals"])
            ).rowcount
        if dry_run:
            with purge_transaction(conn, dry_run=True):
                counts["actions"] = conn.execute(_REHEARSE_ACTIONS, (cutoffs["actions"],)).rowcount
            with purge_transaction(conn, dry_run=True):
                counts["events"] = sum(
                    conn.execute(statement, (cutoffs["events"],)).rowcount for statement in _REHEARSE_EVENTS
                )
    if not dry_run:
        counts["actions"] = ActionJournal(app.ctx.db, clock=clock).purge_terminal(
            older_than_days=days["actions"]
        )
        counts["events"] = app.ctx.events.prune(days["events"])

    connectors: dict[str, dict[str, int]] = {}
    skipped: dict[str, str] = {}
    problems: list[str] = []
    enabled = set(config.enabled_connectors())
    for name, period in sorted(config.retention_connector_days().items()):
        if name not in KNOWN_CONNECTORS:
            problems.append(f"[retention.connectors] {name} is not a known connector")
            continue
        if name not in enabled:
            skipped[name] = "not enabled"
            continue
        try:
            hook = retention_hook(name)
        except Exception as exc:
            problems.append(f"{name}: its retention hook cannot be loaded ({type(exc).__name__}: {exc})")
            continue
        if hook is None:
            problems.append(f"{name} has no retention hook; remove [retention.connectors] {name}")
            continue
        older_than = moment - timedelta(days=period)
        cutoffs[f"connector:{name}"] = iso(older_than)
        try:
            connectors[name] = _checked_counts(
                name, hook.purge(app.ctx, older_than=older_than, dry_run=dry_run)
            )
        except SwitchboardError as exc:
            problems.append(f"{name}: {exc}")
        except Exception as exc:
            log.exception("retention hook %s failed", name)
            problems.append(f"{name}: the retention hook failed ({type(exc).__name__})")
    if not dry_run:
        app.ctx.audit.append(
            "data.purged", counts=counts, connectors=connectors, problems=len(problems), cutoffs=cutoffs
        )
    return PurgeReport(dry_run, cutoffs, counts, connectors, skipped, tuple(problems))


# ---- audit archives ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationReport:
    archive: Path
    archive_sha256: str
    previous_head: str
    records: int
    head: str


@dataclass(frozen=True)
class AuditSegment:
    name: str
    records: int
    head: str


@dataclass(frozen=True)
class AuditChainReport:
    """The live log and every archive it links, newest first."""

    segments: tuple[AuditSegment, ...]
    problems: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def records(self) -> int:
        return sum(segment.records for segment in self.segments)


@dataclass(frozen=True)
class _RotationLink:
    archive: str
    sha256: str
    previous_head: str
    previous_records: int


def rotate_audit(app: App, *, clock: Clock = now) -> RotationReport:
    return rotate_audit_log(app.ctx.audit.path, app.ctx.data_dir / ARCHIVE_DIR, clock=clock)


def rotate_audit_log(audit_path: Path, archive_dir: Path, *, clock: Clock = now) -> RotationReport:
    """Archive the current log privately and restart it with a record linking the archive.

    The new first record keeps ``prev`` at the genesis value, so ``audit verify`` still checks the new log on
    its own; it names the archive, the archive's SHA-256 digest, and the previous head and record count.
    Appends from other processes wait on the log's lock and continue the new chain afterwards.
    """
    with _locked_audit(audit_path, exclusive=True) as fd:
        if fd is None or os.fstat(fd).st_size == 0:
            raise LifecycleError("the audit log is empty; there is nothing to rotate")
        directory = ensure_private_dir(archive_dir)
        staged = directory / f".rotating-{secrets.token_hex(8)}.jsonl"
        try:
            digest = _copy_fd(fd, staged)
            # Check the staged copy, not the live file, which other readers may lock while we hold it.
            current = AuditLog(staged).verify()
            if not current.ok:
                raise LifecycleError(
                    f"the audit log does not verify ({current.problem}); investigate before rotating"
                )
            moment = clock()
            name = f"audit-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{current.head[:16]}.jsonl"
            archive = directory / name
            try:
                os.link(staged, archive)
            except FileExistsError:
                raise LifecycleError(f"{archive} already exists; try again in a second") from None
        finally:
            staged.unlink(missing_ok=True)
        _sync_directory(directory)
        record: dict[str, Any] = {
            "ts": iso(moment),
            "kind": ROTATION_KIND,
            "archive": name,
            "archive_sha256": digest.sha256,
            "archive_bytes": digest.size,
            "previous_head": current.head,
            "previous_records": current.records,
            "prev": GENESIS,
        }
        record["hash"] = sha256_hex(canonical_json(record))
        # A crash between these two steps leaves a complete archive that nothing links; checks report it.
        os.ftruncate(fd, 0)
        _write_all(fd, (canonical_json(record) + "\n").encode("utf-8"), 0)
        os.fsync(fd)
    return RotationReport(archive, digest.sha256, current.head, current.records, str(record["hash"]))


def _first_record(path: Path) -> dict[str, Any] | None:
    with path.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json_loads(line)
            except (ValueError, RecursionError):
                return None
            return value if isinstance(value, dict) else None
    return None


def _rotation_link(path: Path, problems: list[str]) -> _RotationLink | None:
    first = _first_record(path)
    if first is None or first.get("kind") != ROTATION_KIND:
        return None
    archive = first.get("archive")
    digest = first.get("archive_sha256")
    head = first.get("previous_head")
    records = first.get("previous_records")
    if (
        not isinstance(archive, str)
        or not _ARCHIVE_NAME.fullmatch(archive)
        or not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or not isinstance(head, str)
        or not _HASH.fullmatch(head)
        or not _whole(records)
    ):
        problems.append(f"{path.name} starts with a malformed rotation record")
        return None
    return _RotationLink(archive, digest, head, records)


def verify_audit_chain(audit_path: Path, archive_dir: Path) -> AuditChainReport:
    """Verify the live log, then follow each rotation record to its archive and verify that too."""
    live = AuditLog(audit_path).verify()
    if not live.ok:
        return AuditChainReport((), (f"{audit_path.name}: {live.problem}",), ())
    segments = [AuditSegment(audit_path.name, live.records, live.head)]
    problems: list[str] = []
    warnings: list[str] = []
    linked: set[str] = set()
    current = audit_path
    while (link := _rotation_link(current, problems)) is not None:
        if len(linked) >= MAX_LINKED_ARCHIVES:
            problems.append(f"more than {MAX_LINKED_ARCHIVES} linked archives")
            break
        if link.archive in linked:
            problems.append(f"{link.archive} is linked more than once")
            break
        linked.add(link.archive)
        path = archive_dir / link.archive
        try:
            digest = _digest_file(path)
        except FileNotFoundError:
            problems.append(f"{current.name} links {link.archive}, which is missing")
            break
        except (OSError, LifecycleError) as exc:
            problems.append(f"{link.archive} cannot be read safely: {exc}")
            break
        if digest.sha256 != link.sha256:
            problems.append(f"{link.archive} does not match the digest recorded in {current.name}")
            break
        result = AuditLog(path).verify()
        if not result.ok:
            problems.append(f"{link.archive}: {result.problem}")
            break
        if result.head != link.previous_head or result.records != link.previous_records:
            problems.append(f"{link.archive} does not end at the head recorded in {current.name}")
            break
        segments.append(AuditSegment(link.archive, result.records, result.head))
        current = path
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        for name in sorted(os.listdir(archive_dir)):
            if _ARCHIVE_NAME.fullmatch(name) and name not in linked:
                warnings.append(f"{name} is not linked from the current chain")
    return AuditChainReport(tuple(segments), tuple(problems), tuple(warnings))


# ---- audit anchors ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditAnchor:
    """A record's position in the linked chain and its hash, to keep where Switchboard cannot write.

    Positions count from the first record of the oldest linked archive, rotation records included, so an
    anchor names the same record after later rotations.
    """

    sequence: int
    hash: str

    def __str__(self) -> str:
        return f"{self.sequence}:{self.hash}"


def parse_audit_anchor(text: str) -> AuditAnchor:
    """Parse ``SEQ:HASH``, the form ``switchboard audit anchor`` prints."""
    match = _ANCHOR.fullmatch(text.strip().lower())
    if match is None:
        raise LifecycleError("an anchor is SEQ:HASH, a record number and a 64-digit hexadecimal hash")
    return AuditAnchor(int(match.group(1)), match.group(2))


def current_audit_anchor(audit_path: Path, archive_dir: Path) -> AuditAnchor:
    """Verify the linked chain while appends wait, and return the position and hash of its last record."""
    with _locked_audit(audit_path, exclusive=False) as fd:
        report = _locked_chain(fd, audit_path, archive_dir)
    if not report.ok:
        raise LifecycleError(
            f"the audit chain does not verify ({'; '.join(report.problems)}); investigate before anchoring it"
        )
    if report.records == 0:
        raise LifecycleError("the audit log is empty; there is nothing to anchor yet")
    return AuditAnchor(report.records, report.segments[0].head)


def check_audit_anchors(
    audit_path: Path, archive_dir: Path, anchors: Iterable[AuditAnchor]
) -> AuditChainReport:
    """Verify the linked chain, then check that each anchored record is still there with the same hash.

    A chain shorter than an anchor lost records from its end or was replaced. A different hash at the
    anchored position means the chain was rewritten at or before it. Positions in a chain that does not
    verify are unknown, so its anchors are reported as not checked.
    """
    wanted = list(dict.fromkeys(anchors))
    with _locked_audit(audit_path, exclusive=False) as fd:
        report = _locked_chain(fd, audit_path, archive_dir)
        if not report.ok:
            problems = (*report.problems, f"{len(wanted)} anchor(s) not checked: the chain must verify first")
            return AuditChainReport(report.segments, problems, report.warnings)
        found = _hashes_at(report, audit_path, archive_dir, {anchor.sequence for anchor in wanted})
    mismatches: list[str] = []
    for anchor in wanted:
        if anchor.sequence > report.records:
            mismatches.append(
                f"anchor {anchor}: the chain has only {report.records} records; "
                "records were removed from its end, or the log was replaced"
            )
        elif found.get(anchor.sequence) != anchor.hash:
            mismatches.append(
                f"anchor {anchor}: record {anchor.sequence} has a different hash; "
                "the chain was rewritten at or before it"
            )
    return AuditChainReport(report.segments, tuple(mismatches), report.warnings)


def _locked_chain(fd: int | None, audit_path: Path, archive_dir: Path) -> AuditChainReport:
    """Verify the chain while ``fd`` holds the live log's lock; without a live log the chain is empty."""
    if fd is None:
        return AuditChainReport((), (), ())
    return verify_audit_chain(audit_path, archive_dir)


def _hashes_at(
    report: AuditChainReport, audit_path: Path, archive_dir: Path, positions: set[int]
) -> dict[int, str]:
    """Read the hash at each position of a verified chain, in one pass over each segment holding one."""
    found: dict[int, str] = {}
    offset = 0
    for newest_first in reversed(range(len(report.segments))):
        segment = report.segments[newest_first]
        path = archive_dir / segment.name if newest_first else audit_path
        local = {position - offset for position in positions if offset < position <= offset + segment.records}
        if local:
            for number, record in enumerate(AuditLog(path).records(), start=1):
                if number in local:
                    found[offset + number] = str(record.get("hash"))
                if number >= max(local):
                    break
        offset += segment.records
    return found
