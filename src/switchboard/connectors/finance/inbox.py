"""The import inbox: a directory you own where you save statement files for import.

Safety rules:

* The directory must already exist, be a real directory rather than a symbolic link, belong to you, and
  not be writable by other users, who could otherwise plant files in your ledger.
* Only regular files directly inside it with a ``.ofx``, ``.qfx``, or ``.csv`` extension are read; they
  are opened with ``O_NOFOLLOW``, so a symbolic link swapped in at the last moment is refused. Hidden
  files, subdirectories, and other extensions are left alone.
* Imported files move to ``processed/`` and unreadable ones to ``failed/``, created with mode 700.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from switchboard.connectors.finance.models import FormatError
from switchboard.errors import ToolError

SUPPORTED_SUFFIXES = frozenset({".ofx", ".qfx", ".csv"})
PROCESSED = "processed"
FAILED = "failed"
MAX_FILE_BYTES = 20 * 1024 * 1024


def inbox_problems(path: Path, *, strict: bool = False) -> list[str]:
    """Why ``path`` is unsafe or unusable as the inbox; ``strict`` also flags read access by others."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [f"import_dir {path} does not exist; create it with: mkdir -m 700 {path}"]
    except OSError as exc:
        return [f"import_dir {path} cannot be inspected: {exc.strerror}"]
    if stat.S_ISLNK(info.st_mode):
        return [f"import_dir {path} is a symbolic link; point import_dir at the real directory"]
    if not stat.S_ISDIR(info.st_mode):
        return [f"import_dir {path} is not a directory"]
    problems: list[str] = []
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        problems.append(f"import_dir {path} is not owned by you")
    if info.st_mode & 0o022:
        problems.append(f"import_dir {path} is writable by other users; run: chmod 700 {path}")
    elif strict and info.st_mode & 0o044:
        problems.append(f"import_dir {path} is readable by other users; run: chmod 700 {path}")
    return problems


def open_inbox(path: Path) -> Path:
    """Return ``path`` when it is safe to import from; raise ToolError otherwise."""
    problems = inbox_problems(path)
    if problems:
        raise ToolError(problems[0])
    return path


def scan(inbox: Path) -> tuple[list[Path], list[tuple[str, str]]]:
    """Files to import, sorted by name, and ``(name, reason)`` for entries that are left alone."""
    files: list[Path] = []
    ignored: list[tuple[str, str]] = []
    with os.scandir(inbox) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name.startswith(".") or entry.is_dir(follow_symlinks=False):
                continue
            if not entry.is_file(follow_symlinks=False):
                ignored.append((entry.name, "not a regular file (symbolic links are refused)"))
            elif Path(entry.name).suffix.lower() not in SUPPORTED_SUFFIXES:
                ignored.append((entry.name, "unsupported extension; use .ofx, .qfx, or .csv"))
            else:
                files.append(Path(entry.path))
    return files, ignored


def read_file(path: Path, *, follow_symlinks: bool = False, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read a regular file of at most ``limit`` bytes."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FormatError(f"cannot open the file: {exc.strerror}") from exc
    try:
        # Check before fdopen, which refuses a directory without closing the descriptor.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FormatError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise FormatError(f"the file is larger than {limit // (1024 * 1024)} MB")
    return data


def subdirectory(inbox: Path, name: str) -> Path:
    """``inbox/name``, created with mode 700 if needed; refuse a link or a file in its place."""
    target = inbox / name
    with contextlib.suppress(FileExistsError):
        target.mkdir(mode=0o700)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{name}/ inside import_dir must be a real directory, not a link or a file")
    return target


def move_to(path: Path, folder: Path) -> Path:
    """Move ``path`` into ``folder`` without overwriting: ``statement.1.ofx`` when the name is taken."""
    target = folder / path.name
    counter = 1
    while target.exists() or target.is_symlink():
        target = folder / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    path.replace(target)
    return target
