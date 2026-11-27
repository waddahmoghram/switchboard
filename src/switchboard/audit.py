"""Append-only, hash-chained audit log.

Every record stores the hash of the previous record (``prev``) and its own hash over its canonical
JSON (``hash``). ``verify`` detects edits, interior deletions, and reordering. Records cut from the end
show only against an anchor kept elsewhere (``switchboard audit anchor``, then ``audit verify --anchor``).
Someone who can rewrite the whole file can recompute its chain.

Records are redacted before writing: values under keys that look like credentials are replaced, string
values lose recognizable secrets (bearer tokens, private keys, known token formats, long random runs),
and long strings are then abbreviated with a prefix, length, and short hash. Fields named in
``[audit] hash_only`` keep only the length and hash. Other message text and prefixes remain personal data.

Each append takes an exclusive ``flock`` on a freshly opened descriptor. Such locks belong to the open
file description, so they serialize writers across threads of one process as well as across processes.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from switchboard.errors import AuditError
from switchboard.locking import fcntl
from switchboard.util import canonical_json, now_iso, sha256_hex

GENESIS = "0" * 64
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|authori[sz]ation|cookie|api[_-]?key|credential|private[_-]?key", re.I
)
# Short names match whole key segments only: ``card_pin`` and ``userPin``, not ``pinned`` or ``mapping``.
_SENSITIVE_SEGMENTS = frozenset(
    {"otp", "totp", "hotp", "mfa", "2fa", "pin", "session", "private", "auth", "cvv", "cvc"}
)
_KEY_SEGMENT = re.compile(r"[A-Z]+[0-9]*(?![a-z])|[A-Z]?[a-z0-9]+")
# Values under keys whose last segment names an identifier or digest keep long hex and base64url runs.
_IDENTIFIER_SEGMENTS = frozenset(
    {
        "id",
        "ids",
        "uid",
        "uuid",
        "hash",
        "digest",
        "sha1",
        "sha256",
        "sha512",
        "checksum",
        "fingerprint",
        "head",
        "binding",
        "etag",
        "revision",
        "action",
        "target",
    }
)
_MAX_STRING = 300
_MAX_DEPTH = 6
_BLOCK_SIZE = 8192


def _api_key(match: re.Match[str]) -> str:
    return "[redacted:api-key]" if any(char.isdigit() for char in match[0]) else match[0]


def _assigned(match: re.Match[str]) -> str:
    value = match[2]
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return f"{match[1]}{value[0]}[redacted]{value[0]}"
    return f"{match[1]}[redacted]"


def _bearer(match: re.Match[str]) -> str:
    token = match[2]
    if len(token) >= 32 or any(char.isdigit() for char in token):
        return f"{match[1]}[redacted]"
    return match[0]


def _hex(match: re.Match[str]) -> str:
    run = match[0]
    if any(char.isdigit() for char in run) and any(char.isalpha() for char in run):
        return "[redacted:hex]"
    return run


_HEADER_SCHEME = re.compile(r"(?i)(?:basic|bearer|digest|negotiate|token)[ \t]+")


def _header(match: re.Match[str]) -> str:
    """Keep a header's name, quoting, and authentication scheme; redact its value to the quote or line end."""
    quote = match["quote"] or ""
    scheme = _HEADER_SCHEME.match(match["quoted"] if quote else match["bare"])
    return f"{match['open'] or ''}{match['name']}{quote}{scheme[0] if scheme else ''}[redacted]"


_PIECE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _base64url(match: re.Match[str]) -> str:
    """Redact a run that mixes upper case, lower case, and digits in pieces averaging under three."""
    run = match[0]
    mixed = any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)
    pieces = _PIECE.findall(run)
    if mixed and sum(map(len, pieces)) < 3 * len(pieces):
        return "[redacted:base64]"
    return run


_Replacement = str | Callable[[re.Match[str]], str]
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (
        re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.S,
        ),
        "[redacted:private-key]",
    ),
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[redacted:jwt]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://[^\s:/?#@]*):[^\s/?#@]+@"), r"\1:[redacted]@"),
    (
        re.compile(
            r"(?i)([?&;](?:access_token|refresh_token|id_token|token|api_key|apikey|key|secret|client_secret"
            r"|password|passwd|auth|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token"
            r"|x-goog-signature|x-goog-credential)=)[^&#\s\"'<>]+"
        ),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"(?i)(?<![a-z0-9])((?:password|passwd|passphrase|passcode|secret|client[_-]?secret|api[_-]?key"
            r"|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|otp|pin)[\"']?[ \t]*[:=][ \t]*)"
            r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[\"']?[^\s\"',;&<>]+)"
        ),
        _assigned,
    ),
    (
        re.compile(
            r"(?i)(?P<open>[\"'])?\b(?P<name>(?:(?:proxy-)?authorization|x-api-key|x-auth-token"
            r"|(?:set-)?cookie(?=[\"']?[ \t]*:[ \t]*[\"']?[^\s=;,\"']+=))[\"']?[ \t]*:[ \t]*)"
            r"(?:(?P<quote>[\"'])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)"
            r"|(?P<bare>(?(open)(?:(?!(?P=open))[^\r\n])*|\S[^\r\n]*)))"
        ),
        _header,
    ),
    (re.compile(r"(?i)\b(bearer[ \t]+)([A-Za-z0-9._~+/-]{16,}=*)"), _bearer),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,})(?![A-Za-z0-9_])"
        ),
        "[redacted:github-token]",
    ),
    (re.compile(r"(?<![A-Za-z0-9-])xox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted:slack-token]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"), "[redacted:aws-key-id]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
            r"|AIza[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])"
        ),
        _api_key,
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])((?:bot)?[0-9]{6,12}):[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"),
        r"\1:[redacted]",
    ),
)
_RANDOM_RUNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?![A-Za-z0-9])"), _hex),
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"), _base64url),
)


def _scrub(text: str, *, identifier: bool) -> str:
    """Replace recognizable secrets inside ``text``; ``identifier`` keeps long hex and base64url runs."""
    for pattern, replacement in _VALUE_PATTERNS if identifier else _VALUE_PATTERNS + _RANDOM_RUNS:
        text = pattern.sub(replacement, text)
    return text


def _digest(text: str) -> str:
    return f"[{len(text)} chars, sha256:{sha256_hex(text)[:16]}]"


def redact(value: Any, depth: int = 0, *, hash_only: Iterable[str] = ()) -> Any:
    """Return a copy of ``value`` safe to persist in the audit log.

    Values under credential-like keys are replaced. Strings lose recognizable secrets first; then long ones
    keep a prefix, their length, and a short hash of the scrubbed text. Strings at any depth under a key
    named in ``hash_only`` (case-insensitive) keep only the length and hash.
    """
    hashed_keys = frozenset(name.lower() for name in hash_only)
    return _redact(value, depth, hashed_keys, identifier=False, hashed=False)


def _redact(value: Any, depth: int, hash_only: frozenset[str], *, identifier: bool, hashed: bool) -> Any:
    if depth > _MAX_DEPTH:
        return "[nested too deeply]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            segments = [segment.lower() for segment in _KEY_SEGMENT.findall(name)]
            if _SENSITIVE_KEY.search(name) or not _SENSITIVE_SEGMENTS.isdisjoint(segments):
                result[name] = "[redacted]"
                continue
            result[name] = _redact(
                item,
                depth + 1,
                hash_only,
                identifier=bool(segments) and segments[-1] in _IDENTIFIER_SEGMENTS,
                hashed=hashed or name.lower() in hash_only,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _redact(item, depth + 1, hash_only, identifier=identifier, hashed=hashed) for item in value[:50]
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = _scrub(value if isinstance(value, str) else str(value), identifier=identifier)
    if hashed:
        return _digest(text)
    if len(text) > _MAX_STRING:
        return f"{text[:120]}…{_digest(text)}"
    return text


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    head: str
    problem: str | None = None


class AuditLog:
    def __init__(self, path: Path, *, hash_only: Iterable[str] = ()) -> None:
        self.path = path
        self.hash_only = frozenset(name.lower() for name in hash_only)

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": now_iso(), "kind": kind}
        record.update(redact(fields, hash_only=self.hash_only))
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
        """Return the last ``count`` records, reading the file backwards only as far as they reach.

        Damage before those records does not show here; ``verify`` reads everything. Anything unusual in
        the lines it reads (a partial or malformed line, invalid UTF-8, a bare carriage return) falls back to
        a full scan, so errors and results match ``records``.
        """
        if count < 1:
            return []
        if not self.path.exists():
            return []
        with self.path.open("rb") as handle:
            recent = _tail_records(handle, count)
        if recent is None:
            return list(deque(self.records(), maxlen=count))
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
        step = min(_BLOCK_SIZE, position)
        position -= step
        handle.seek(position)
        buffer = handle.read(step) + buffer
        stripped = buffer.rstrip(b"\n")
        cut = stripped.rfind(b"\n")
        if cut != -1:
            return stripped[cut + 1 :]
    stripped = buffer.rstrip(b"\n")
    return stripped or None


def _tail_records(handle: IO[bytes], count: int) -> list[dict[str, Any]] | None:
    """Parse up to ``count`` records from the end of ``handle``, or return None if a line needs a full scan.

    A line needs one when ``AuditLog.records`` would read it differently or reject it: a bare carriage
    return (a line break in text mode), invalid UTF-8, invalid JSON, or JSON that is not an object.
    """
    recent: list[dict[str, Any]] = []
    for line in _lines_backwards(handle):
        if line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line:
            return None
        try:
            text = line.decode("utf-8")
            if not text.strip():
                continue
            record = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(record, dict):
            return None
        recent.append(record)
        if len(recent) == count:
            break
    recent.reverse()
    return recent


def _lines_backwards(handle: IO[bytes]) -> Iterator[bytes]:
    """Yield the newline-separated lines of a binary file from last to first, reading blocks from the end.

    The first line yielded is whatever follows the final newline: empty when the file ends with one.
    """
    position = handle.seek(0, os.SEEK_END)
    pieces: list[bytes] = []  # the line being assembled, its last piece first
    while position > 0:
        step = min(_BLOCK_SIZE, position)
        position -= step
        handle.seek(position)
        parts = handle.read(step).split(b"\n")
        pieces.append(parts[-1])
        if len(parts) > 1:
            yield b"".join(reversed(pieces))
            yield from reversed(parts[1:-1])
            pieces = [parts[0]]
    yield b"".join(reversed(pieces))
