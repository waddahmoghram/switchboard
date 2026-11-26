"""Append-only, hash-chained audit log.

Every record stores the hash of the previous record (``prev``) and its own hash over its canonical
JSON (``hash``). ``verify`` detects edits, interior deletions, and reordering. Tail truncation needs an
independently stored head hash to detect. Someone who can rewrite the whole file can recompute its chain.

Records are redacted before writing: keys that look like credentials are replaced, and long strings
are abbreviated with a prefix, length, and short hash. Short message bodies and prefixes remain personal data.

Each append takes an exclusive ``flock`` on a freshly opened descriptor. Such locks belong to the open
file description, so they serialize writers across threads of one process as well as across processes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from switchboard.errors import AuditError
from switchboard.locking import fcntl
from switchboard.util import canonical_json, now_iso, sha256_hex

GENESIS = "0" * 64
_SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|authori[sz]ation|cookie|api[_-]?key|credential", re.I)
_MAX_STRING = 300
_MAX_DEPTH = 6


def redact(value: Any, depth: int = 0) -> Any:
    """Return a copy of ``value`` safe to persist in the audit log."""
    if depth > _MAX_DEPTH:
        return "[nested too deeply]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value[:50]]
    if isinstance(value, str) and len(value) > _MAX_STRING:
        return f"{value[:120]}…[{len(value)} chars, sha256:{sha256_hex(value)[:16]}]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    head: str
    problem: str | None = None


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": now_iso(), "kind": kind}
        record.update(redact(fields))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                last = _read_last_line(handle)
                record["prev"] = GENESIS if last is None else json.loads(last)["hash"]
                record["hash"] = sha256_hex(canonical_json(record))
                handle.write((canonical_json(record) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return record

    def records(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            try:
                for number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError as exc:
                        raise AuditError(f"record {number}: invalid JSON") from exc
                    if not isinstance(record, dict):
                        raise AuditError(f"record {number}: expected a JSON object")
                    yield record
            except UnicodeError as exc:
                raise AuditError("audit log is not valid UTF-8") from exc

    def tail(self, count: int) -> list[dict[str, Any]]:
        if count < 1:
            return []
        recent: list[dict[str, Any]] = []
        for record in self.records():
            recent.append(record)
            if len(recent) > count:
                recent.pop(0)
        return recent

    def head(self) -> str:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return GENESIS
        # fdopen leaves a descriptor it refuses (a directory) open, so close it here on every path.
        try:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                # Appenders hold an exclusive lock: no thread or process reads a half-written line.
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                last = _read_last_line(handle)
        finally:
            os.close(fd)
        return GENESIS if last is None else str(json.loads(last)["hash"])

    def verify(self) -> VerifyResult:
        expected_prev = GENESIS
        count = 0
        try:
            for count, record in enumerate(self.records(), start=1):
                if record.get("prev") != expected_prev:
                    return VerifyResult(
                        False, count, expected_prev, f"record {count}: chain broken (prev mismatch)"
                    )
                claimed = record.get("hash")
                body = {key: value for key, value in record.items() if key != "hash"}
                if sha256_hex(canonical_json(body)) != claimed:
                    return VerifyResult(
                        False, count, expected_prev, f"record {count}: content does not match its hash"
                    )
                expected_prev = str(claimed)
        except AuditError as exc:
            return VerifyResult(False, count, expected_prev, str(exc))
        return VerifyResult(True, count, expected_prev)


def _read_last_line(handle: IO[bytes]) -> bytes | None:
    """Return the last non-empty line of a binary file without reading the whole file."""
    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    if position == 0:
        return None
    buffer = b""
    while position > 0:
        step = min(8192, position)
        position -= step
        handle.seek(position)
        buffer = handle.read(step) + buffer
        stripped = buffer.rstrip(b"\n")
        cut = stripped.rfind(b"\n")
        if cut != -1:
            return stripped[cut + 1 :]
    stripped = buffer.rstrip(b"\n")
    return stripped or None
