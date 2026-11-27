"""Private-report tests; every operating-system operation is substituted."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.connectors.monitoring import files
from switchboard.connectors.monitoring.files import MAX_REPORT_BYTES, CheckError, read_report
from switchboard.connectors.monitoring.settings import BackupCheck
from tests.fixtures.monitoring import PRIVATE_MARKER

CHECK = BackupCheck("backup", Path("/synthetic-private/status"), "backup.json", 3600)


class ReportFiles:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.position = 0
        self.directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1234)
        self.file = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1234, st_size=len(body), st_nlink=1)
        self.opened: list[tuple[Any, int, int | None]] = []
        self.closed: list[int] = []
        self.read_sizes: list[int] = []
        self.failure: OSError | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(files.os, "getuid", lambda: 1234)
        monkeypatch.setattr(files.os, "open", self.open)
        monkeypatch.setattr(files.os, "fstat", self.fstat)
        monkeypatch.setattr(files.os, "read", self.read)
        monkeypatch.setattr(files.os, "close", self.closed.append)

    def open(self, path: Any, flags: int, *, dir_fd: int | None = None) -> int:
        self.opened.append((path, flags, dir_fd))
        if self.failure is not None and dir_fd is not None:
            raise self.failure
        return 10 if dir_fd is None else 11

    def fstat(self, fd: int) -> Any:
        return self.directory if fd == 10 else self.file

    def read(self, fd: int, size: int) -> bytes:
        assert fd == 11
        self.read_sizes.append(size)
        part = self.body[self.position : self.position + size]
        self.position += len(part)
        return part


def test_report_file_is_read_only_descriptor_relative_private_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ReportFiles(b'{"synthetic":true}')
    fake.install(monkeypatch)
    assert read_report(CHECK) == fake.body
    [(directory, directory_flags, no_parent), (filename, flags, parent)] = fake.opened
    assert directory == CHECK.directory
    assert no_parent is None
    assert filename == CHECK.filename
    assert parent == 10
    assert directory_flags & os.O_DIRECTORY
    assert directory_flags & os.O_NOFOLLOW
    assert flags & os.O_NOFOLLOW
    assert flags & os.O_NONBLOCK
    for _path, opened_flags, _parent in fake.opened:
        assert opened_flags & os.O_ACCMODE == os.O_RDONLY
        assert not opened_flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
    assert max(fake.read_sizes) == MAX_REPORT_BYTES + 1
    assert fake.closed == [11, 10]


@pytest.mark.parametrize(
    ("target", "field", "value", "code"),
    [
        ("directory", "st_mode", stat.S_IFDIR | 0o750, "report_directory_not_private"),
        ("directory", "st_uid", 999, "report_directory_not_private"),
        ("directory", "st_mode", stat.S_IFLNK | 0o700, "report_directory_not_private"),
        ("file", "st_mode", stat.S_IFREG | 0o640, "report_not_private"),
        ("file", "st_uid", 999, "report_not_private"),
        ("file", "st_nlink", 2, "report_not_private"),
        ("file", "st_mode", stat.S_IFLNK | 0o600, "report_not_regular"),
        ("file", "st_mode", stat.S_IFIFO | 0o600, "report_not_regular"),
        ("file", "st_mode", stat.S_IFCHR | 0o600, "report_not_regular"),
        ("file", "st_size", MAX_REPORT_BYTES + 1, "report_too_large"),
        ("file", "st_size", -1, "report_too_large"),
    ],
)
def test_unsafe_metadata_is_rejected_before_any_read(
    monkeypatch: pytest.MonkeyPatch, target: str, field: str, value: int, code: str
) -> None:
    fake = ReportFiles()
    setattr(getattr(fake, target), field, value)
    fake.install(monkeypatch)
    with pytest.raises(CheckError, match=code):
        read_report(CHECK)
    assert fake.read_sizes == []
    assert fake.closed == ([10] if target == "directory" else [11, 10])


def test_actual_read_limit_catches_growth_after_metadata_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ReportFiles(b"x" * (MAX_REPORT_BYTES + 1))
    fake.file.st_size = 1
    fake.install(monkeypatch)
    with pytest.raises(CheckError, match="report_too_large"):
        read_report(CHECK)
    assert fake.read_sizes == [MAX_REPORT_BYTES + 1]
    assert fake.closed == [11, 10]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError(PRIVATE_MARKER), "report_missing"),
        (PermissionError(PRIVATE_MARKER), "report_permission_denied"),
        (OSError(PRIVATE_MARKER), "report_unavailable"),
    ],
)
def test_open_failures_are_sanitized_and_the_directory_is_closed(
    monkeypatch: pytest.MonkeyPatch, error: OSError, code: str
) -> None:
    fake = ReportFiles()
    fake.failure = error
    fake.install(monkeypatch)
    with pytest.raises(CheckError, match=code) as caught:
        read_report(CHECK)
    assert PRIVATE_MARKER not in str(caught.value)
    assert fake.closed == [10]
    assert fake.read_sizes == []


def test_platforms_without_no_follow_protection_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ReportFiles()
    fake.install(monkeypatch)
    monkeypatch.delattr(files.os, "O_NOFOLLOW")
    with pytest.raises(CheckError, match="report_security_unavailable"):
        read_report(CHECK)
    assert fake.opened == []
