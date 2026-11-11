"""Descriptor-relative live reads, with no traversal, cache, persistent descriptors, or writes."""

from __future__ import annotations

import os
import stat
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from switchboard.connectors._archive import ImportFormatError, decode_text, private_import_dir
from switchboard.connectors.documents.settings import MAX_FILENAME_BYTES, Collection, Settings
from switchboard.errors import ToolError


class NoteUnavailable(ToolError):
    """A supported filename cannot currently supply safe, complete note text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"note unavailable ({reason}); use an owned private regular text file")


class SearchBudgetExhausted(Exception):
    """The remaining byte budget cannot establish a complete file read."""


@dataclass
class ReadBudget:
    remaining: int
    consumed: int = 0

    def consume(self, size: int) -> None:
        self.remaining -= size
        self.consumed += size


@dataclass(frozen=True)
class Candidate:
    name: str
    info: os.stat_result


@dataclass(frozen=True)
class Note:
    text: str
    info: os.stat_result
    size_bytes: int


def supported_name(name: str, extensions: tuple[str, ...]) -> bool:
    """Accept visible basenames only, including mixed-case declared suffixes."""
    if (
        not name
        or name.startswith(".")
        or any(char in name for char in ("/", "\\", ":"))
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
        or not name.lower().endswith(extensions)
    ):
        return False
    try:
        return len(name.encode("utf-8")) <= MAX_FILENAME_BYTES
    except UnicodeError:
        return False


def _private_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _check_file(info: os.stat_result, maximum: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
        or info.st_nlink != 1
    ):
        raise NoteUnavailable("unsafe_file")
    if info.st_size > maximum:
        raise NoteUnavailable("too_large")


def _open_root(collection: Collection) -> int:
    """Walk every absolute path component without following links; fail closed off supported systems."""
    if os.name != "posix" or not all(
        hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        raise ToolError("documents requires descriptor-relative local file access with no-follow support")
    descriptor: int | None = None
    try:
        private_import_dir(collection.path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        for component in collection.path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not _private_directory(os.fstat(descriptor)):
            raise ToolError("collection permissions changed")
        return descriptor
    except (OSError, ToolError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ToolError(
            "collection unavailable; use an owned directory with mode 700 and no symbolic links in its path"
        ) from exc


class LiveCollection:
    def __init__(self, descriptor: int, collection: Collection, settings: Settings) -> None:
        self.descriptor = descriptor
        self.collection = collection
        self.settings = settings
        self.root_info = os.fstat(descriptor)
        self.observed: dict[str, os.stat_result] = {}

    def inspect(self, name: str) -> os.stat_result:
        try:
            info = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except OSError as exc:
            raise NoteUnavailable("unavailable") from exc
        _check_file(info, self.settings.max_file_bytes)
        return info

    def candidates(self) -> tuple[list[Candidate], Counter[str], int]:
        """Bound enumeration itself, including hidden, unsupported, and non-regular entries."""
        found: list[Candidate] = []
        skipped: Counter[str] = Counter()
        examined = 0
        try:
            with os.scandir(self.descriptor) as entries:
                for entry in entries:
                    examined += 1
                    if examined > self.settings.max_entries:
                        raise ToolError("collection entry limit exceeded; use a smaller dedicated folder")
                    if not supported_name(entry.name, self.collection.extensions):
                        skipped["unsupported_name"] += 1
                        continue
                    try:
                        info = self.inspect(entry.name)
                    except NoteUnavailable as exc:
                        skipped[exc.reason] += 1
                        continue
                    found.append(Candidate(entry.name, info))
        except OSError as exc:
            raise ToolError("collection cannot currently be listed") from exc
        return sorted(found, key=lambda item: item.name), skipped, examined

    def read(self, name: str, budget: ReadBudget) -> Note:
        """Check both advertised and actual sizes, then validate the opened file and its current name."""
        if not supported_name(name, self.collection.extensions):
            raise ToolError("choose a supported top-level note filename, not a path or hidden file")
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                _check_file(before, self.settings.max_file_bytes)
                if before.st_size >= budget.remaining:
                    raise SearchBudgetExhausted
                maximum = min(self.settings.max_file_bytes + 1, budget.remaining)
                raw = handle.read(maximum)
                budget.consume(len(raw))
                if len(raw) > self.settings.max_file_bytes:
                    raise NoteUnavailable("too_large")
                if len(raw) == maximum:
                    raise SearchBudgetExhausted
                after = os.fstat(handle.fileno())
                _check_file(after, self.settings.max_file_bytes)
                if _fingerprint(before) != _fingerprint(after) or len(raw) != after.st_size:
                    raise NoteUnavailable("changed")
            current = self.inspect(name)
            if _fingerprint(current) != _fingerprint(after):
                raise NoteUnavailable("changed")
        except OSError as exc:
            raise NoteUnavailable("unavailable") from exc
        try:
            text = decode_text(raw)
        except ImportFormatError as exc:
            raise NoteUnavailable("invalid_text") from exc
        if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159 for char in text):
            raise NoteUnavailable("invalid_text")
        self.observed[name] = current
        return Note(text, current, len(raw))

    def revalidate(self) -> None:
        """Do not return accumulated results after observed revocation, replacement, deletion, or edits."""
        check = _open_root(self.collection)
        try:
            current_root = os.fstat(check)
            if not _private_directory(os.fstat(self.descriptor)) or (
                current_root.st_dev,
                current_root.st_ino,
            ) != (self.root_info.st_dev, self.root_info.st_ino):
                raise ToolError("collection changed during the request; retry")
        finally:
            os.close(check)
        for name, previous in self.observed.items():
            if _fingerprint(self.inspect(name)) != _fingerprint(previous):
                raise ToolError("note changed during the request; retry")


@contextmanager
def open_collection(
    collection: Collection, settings: Settings, recheck_allowlist: Callable[[], None]
) -> Iterator[LiveCollection]:
    """Close every descriptor, and recheck authorization before any result leaves the handler."""
    descriptor = _open_root(collection)
    try:
        source = LiveCollection(descriptor, collection, settings)
        yield source
        recheck_allowlist()
        source.revalidate()
    finally:
        os.close(descriptor)
