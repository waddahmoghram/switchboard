"""Bounded exports and snapshot storage. Table prefixes are validated; record data uses bound parameters."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import canonical_json, json_loads, now_iso, sha256_hex, truncate

MAX_IMPORT_BYTES = 20_000_000
MAX_ARCHIVE_MEMBERS = 1000
MAX_RECORDS = 50_000
MAX_TEXT_CHARS = 20_000
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,31}")


class ImportFormatError(ToolError):
    """An export is unsupported, malformed, or exceeds a documented limit."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    data: bytes


@dataclass(frozen=True)
class ArchiveRecord:
    kind: str
    label: str
    data: dict[str, Any]


def private_import_dir(directory: Path | None) -> Path:
    if directory is None:
        raise ToolError("set import_dir to a dedicated private folder before importing exports")
    path = directory.expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ToolError(f"cannot inspect the import folder ({exc.strerror})") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ToolError("the import folder must be a real directory you own with mode 700")
    return path


def read_import_file(directory: Path | None, filename: str, suffixes: tuple[str, ...]) -> bytes:
    """Read an owned regular file by basename, with both metadata and actual-read size bounds."""
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or Path(filename).suffix.lower() not in suffixes
    ):
        raise ToolError("choose a supported export filename, not a path")
    path = private_import_dir(directory)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        directory_info = os.fstat(directory_fd)
        if directory_info.st_uid != os.getuid() or directory_info.st_mode & 0o077:
            raise ToolError("the opened import folder must be owned by you with mode 700")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(filename, flags, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ToolError("the export must be an owned regular file, not writable by others")
            if info.st_size > MAX_IMPORT_BYTES:
                raise ImportFormatError(f"exports must not exceed {MAX_IMPORT_BYTES} bytes")
            body = handle.read(MAX_IMPORT_BYTES + 1)
    except OSError as exc:
        raise ToolError(f"cannot read the export ({exc.strerror}); symbolic links are refused") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    if len(body) > MAX_IMPORT_BYTES:
        raise ImportFormatError(f"exports must not exceed {MAX_IMPORT_BYTES} bytes")
    return body


def decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ImportFormatError("the export must use UTF-8 text encoding") from exc


def zip_members(raw: bytes, suffixes: tuple[str, ...]) -> list[ArchiveMember]:
    """Read selected text members with an aggregate inflated-size cap and no filesystem extraction."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportFormatError(f"archives must not exceed {MAX_IMPORT_BYTES} bytes")
    found: list[ArchiveMember] = []
    seen: set[str] = set()
    remaining = MAX_IMPORT_BYTES
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ImportFormatError(f"archives may contain at most {MAX_ARCHIVE_MEMBERS} entries")
            for member in members:
                name = member.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if (
                    "\x00" in name
                    or path.is_absolute()
                    or ".." in path.parts
                    or re.match(r"^[A-Za-z]:", name)
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise ImportFormatError("the archive contains an unsafe member name or symbolic link")
                if member.is_dir() or path.suffix.lower() not in suffixes:
                    continue
                if name.casefold() in seen:
                    raise ImportFormatError("the archive contains duplicate member names")
                seen.add(name.casefold())
                if member.flag_bits & 1:
                    raise ImportFormatError("encrypted archive members are not supported")
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ImportFormatError("only stored or deflated archive members are supported")
                if member.file_size > remaining:
                    raise ImportFormatError("the archive exceeds the total uncompressed text limit")
                with archive.open(member) as handle:
                    body = handle.read(remaining + 1)
                if len(body) > remaining:
                    raise ImportFormatError("the archive exceeds the total uncompressed text limit")
                remaining -= len(body)
                found.append(ArchiveMember(name, body))
    except (zipfile.BadZipFile, RuntimeError, ValueError, zlib.error) as exc:
        raise ImportFormatError("the archive cannot be read safely") from exc
    if not found:
        raise ImportFormatError("the archive contains no supported text exports")
    return found


def _prefix(name: str) -> str:
    if _PREFIX.fullmatch(name) is None:
        raise ValueError("archive table prefixes must be simple connector names")
    return name


def archive_migrations(name: str) -> list[Migration]:
    name = _prefix(name)
    return [
        (
            1,
            (
                f"CREATE TABLE {name}_imports (id INTEGER PRIMARY KEY, file_key TEXT UNIQUE NOT NULL, "
                "filename TEXT NOT NULL, records INTEGER NOT NULL, imported_at TEXT NOT NULL)",
                f"CREATE TABLE {name}_records (id INTEGER PRIMARY KEY, record_key TEXT UNIQUE NOT NULL, "
                "kind TEXT NOT NULL, label TEXT NOT NULL, data_json TEXT NOT NULL, "
                "search_text TEXT NOT NULL, "
                "imported_at TEXT NOT NULL)",
                f"CREATE INDEX {name}_record_kind ON {name}_records(kind, label, id)",
            ),
        )
    ]


class ArchiveStore:
    def __init__(self, ctx: AppContext, name: str) -> None:
        self.ctx = ctx
        self.name = _prefix(name)

    def import_records(
        self,
        file_key: str,
        filename: str,
        records: Sequence[ArchiveRecord],
        *,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ImportFormatError("the export contains no supported records")
        if len(records) > MAX_RECORDS:
            raise ImportFormatError(f"exports may contain at most {MAX_RECORDS} records")
        counts: Counter[str] = Counter()
        inserted = 0
        stamp = now_iso()
        with self.ctx.db.session() as conn, transaction(conn):
            previous = conn.execute(
                f"SELECT id, records FROM {self.name}_imports WHERE file_key = ?",  # noqa: S608
                (file_key,),
            ).fetchone()
            if previous is not None:
                return {
                    "status": "duplicate",
                    "import_id": previous["id"],
                    "records": previous["records"],
                    "inserted": 0,
                }
            for record in records:
                data = json.dumps(record.data, ensure_ascii=True, sort_keys=True, allow_nan=False)
                identity = sha256_hex(
                    canonical_json({"kind": record.kind, "label": record.label, "data": record.data})
                )
                counts[identity] += 1
                key = sha256_hex(f"{identity}:{counts[identity]}")
                cursor = conn.execute(
                    f"INSERT OR IGNORE INTO {self.name}_records "  # noqa: S608
                    "(record_key, kind, label, data_json, search_text, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        record.kind,
                        record.label,
                        data,
                        "\n".join(_strings(record.data)).casefold(),
                        stamp,
                    ),
                )
                inserted += cursor.rowcount
            cursor = conn.execute(
                f"INSERT INTO {self.name}_imports "  # noqa: S608
                "(file_key, filename, records, imported_at) VALUES (?, ?, ?, ?)",
                (file_key, filename, len(records), stamp),
            )
            result = {
                "status": "imported",
                "import_id": cursor.lastrowid,
                "records": len(records),
                "inserted": inserted,
                "historical": True,
            }
            self.ctx.events.emit(
                self.name,
                f"{self.name}.imported",
                {
                    **dict(details or {}),
                    "file": filename,
                    "records": len(records),
                    "inserted": inserted,
                    "historical": True,
                },
                dedupe_key=f"{self.name}:import:{file_key}",
                connection=conn,
            )
        return result

    def search(
        self, query: str = "", *, kind: str | None = None, label: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        clauses = ["instr(search_text, ?) > 0"]
        values: list[Any] = [query.casefold()]
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind)
        if label is not None:
            clauses.append("label = ?")
            values.append(label)
        values.append(limit + 1)
        with self.ctx.db.session() as conn:
            rows = conn.execute(
                f"SELECT id, kind, label, data_json, imported_at FROM {self.name}_records "  # noqa: S608
                f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                values,
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["data"] = _preview(json_loads(item.pop("data_json")))
            item.update(historical=True, untrusted_content=True, preview_only=True)
            items.append(item)
        return {
            "records": items,
            "truncated": len(rows) > limit,
            "historical": True,
            "untrusted_content": True,
        }

    def read(self, record_id: int) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            row = conn.execute(
                f"SELECT id, kind, label, data_json, imported_at FROM {self.name}_records "  # noqa: S608
                "WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise ToolError("no imported record with that id")
        item = dict(row)
        item["data"] = json_loads(item.pop("data_json"))
        item.update(historical=True, untrusted_content=True)
        return item


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _preview(value: Any) -> Any:
    if isinstance(value, str):
        return truncate(value, 240)
    if isinstance(value, dict):
        return {key: _preview(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_preview(item) for item in value]
    return value
