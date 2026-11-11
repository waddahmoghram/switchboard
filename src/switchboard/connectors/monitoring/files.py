"""Bounded read-only access to an owned private completion report."""

from __future__ import annotations

import os
import stat

from .settings import BackupCheck

MAX_REPORT_BYTES = 16384


class CheckError(Exception):
    """A fixed, safe failure code, never external content or an address."""


def read_report(check: BackupCheck) -> bytes:
    """Pin directory and file descriptors, reject links and non-private files, and bound actual reads."""
    if not all(hasattr(os, name) for name in ("getuid", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        raise CheckError("report_security_unavailable")
    directory_fd: int | None = None
    report_fd: int | None = None
    try:
        uid = os.getuid()
        directory_fd = os.open(
            check.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid or directory.st_mode & 0o077:
            raise CheckError("report_directory_not_private")
        report_fd = os.open(
            check.filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(report_fd)
        if not stat.S_ISREG(info.st_mode):
            raise CheckError("report_not_regular")
        if info.st_uid != uid or info.st_mode & 0o077 or info.st_nlink != 1:
            raise CheckError("report_not_private")
        if not 0 <= info.st_size <= MAX_REPORT_BYTES:
            raise CheckError("report_too_large")
        chunks: list[bytes] = []
        remaining = MAX_REPORT_BYTES + 1
        while remaining:
            chunk = os.read(report_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > MAX_REPORT_BYTES:
            raise CheckError("report_too_large")
        return body
    except FileNotFoundError as exc:
        raise CheckError("report_missing") from exc
    except PermissionError as exc:
        raise CheckError("report_permission_denied") from exc
    except OSError as exc:
        raise CheckError("report_unavailable") from exc
    finally:
        if report_fd is not None:
            os.close(report_fd)
        if directory_fd is not None:
            os.close(directory_fd)
