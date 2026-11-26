"""Authored UNRUN synthetic live-file boundary tests; no personal folders or accounts."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest

from switchboard.connectors.documents import filesystem
from switchboard.connectors.documents.filesystem import (
    NoteUnavailable,
    ReadBudget,
    SearchBudgetExhausted,
    open_collection,
    supported_name,
)
from switchboard.connectors.documents.settings import load_settings
from switchboard.errors import ToolError


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _write(root: Path, name: str, raw: bytes = b"synthetic note") -> Path:
    path = root / name
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _source(root: Path, **limits: Any) -> AbstractContextManager[filesystem.LiveCollection]:
    settings = load_settings({"collections": [{"name": "notes", "path": str(root)}], **limits})
    return open_collection(settings.collections[0], settings, lambda: None)


def _changed_info(info: os.stat_result, position: int, value: int) -> os.stat_result:
    fields = list(info)
    fields[position] = value
    return os.stat_result(fields)


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owned_private_regular_files_are_read_live(private_root: Path, mode: int) -> None:
    path = _write(private_root, "note.md")
    path.chmod(mode)
    budget = ReadBudget(100)
    with _source(private_root) as source:
        note = source.read("note.md", budget)
        assert note.text == "synthetic note"
        assert note.size_bytes == budget.consumed == len(b"synthetic note")


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o000, 0o600, 0o1700])
def test_shared_or_unusable_collection_permissions_are_refused(private_root: Path, mode: int) -> None:
    private_root.chmod(mode)
    with pytest.raises(ToolError, match="collection unavailable"), _source(private_root):
        pass


def test_foreign_collection_owner_is_refused(private_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = os.getuid()
    monkeypatch.setattr(filesystem.os, "getuid", lambda: current + 1)
    with pytest.raises(ToolError, match="collection unavailable"), _source(private_root):
        pass


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o000, 0o1600])
def test_notes_must_have_private_nonexecutable_readable_modes(private_root: Path, mode: int) -> None:
    path = _write(private_root, "note.md")
    path.chmod(mode)
    with _source(private_root) as source:
        with pytest.raises(NoteUnavailable):
            source.read("note.md", ReadBudget(100))
        notes, skipped, _ = source.candidates()
        assert notes == []
        assert skipped["unsafe_file"] == 1


def test_foreign_note_owner_is_refused(private_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(private_root, "note.md").stat().st_ino
    actual = os.fstat

    def foreign(descriptor: int) -> os.stat_result:
        info = actual(descriptor)
        return _changed_info(info, stat.ST_UID, info.st_uid + 1) if info.st_ino == target else info

    monkeypatch.setattr(filesystem.os, "fstat", foreign)
    with _source(private_root) as source, pytest.raises(NoteUnavailable, match="unsafe_file"):
        source.read("note.md", ReadBudget(100))


def test_missing_root_is_refused_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(ToolError, match="collection unavailable"), _source(root):
        pass
    assert not root.exists()


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"synthetic")
    root.chmod(0o600)
    with pytest.raises(ToolError, match="collection unavailable"), _source(root):
        pass


def test_symlink_collection_root_is_refused(private_root: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(private_root, target_is_directory=True)
    with pytest.raises(ToolError, match="no symbolic links"), _source(alias):
        pass


def test_symlink_in_an_ancestor_is_refused(private_root: Path, tmp_path: Path) -> None:
    alias = tmp_path / "parent-alias"
    alias.symlink_to(private_root.parent, target_is_directory=True)
    with pytest.raises(ToolError, match="no symbolic links"), _source(alias / private_root.name):
        pass


@pytest.mark.parametrize("relative", [True, False])
def test_note_symlinks_are_never_followed(private_root: Path, relative: bool) -> None:
    target = _write(private_root, "target.md", b"do not follow this link")
    (private_root / "alias.md").symlink_to(target.name if relative else target)
    with _source(private_root) as source:
        with pytest.raises(NoteUnavailable):
            source.read("alias.md", ReadBudget(100))
        notes, skipped, _ = source.candidates()
        assert [item.name for item in notes] == ["target.md"]
        assert skipped["unsafe_file"] == 1


def test_hardlinked_notes_are_refused(private_root: Path, tmp_path: Path) -> None:
    target = _write(tmp_path, "outside.md")
    os.link(target, private_root / "alias.md")
    with _source(private_root) as source, pytest.raises(NoteUnavailable, match="unsafe_file"):
        source.read("alias.md", ReadBudget(100))


def test_directory_and_named_pipe_are_not_regular_notes(private_root: Path) -> None:
    (private_root / "folder.md").mkdir(mode=0o700)
    os.mkfifo(private_root / "pipe.md", mode=0o600)
    with _source(private_root) as source:
        notes, skipped, _ = source.candidates()
        assert notes == []
        assert skipped["unsafe_file"] == 2
        for name in ("folder.md", "pipe.md"):
            with pytest.raises(NoteUnavailable, match="unsafe_file"):
                source.read(name, ReadBudget(100))


def test_refused_and_completed_reads_close_their_descriptors(
    private_root: Path, open_descriptors: Callable[[], int]
) -> None:
    (private_root / "folder.md").mkdir(mode=0o700)
    os.mkfifo(private_root / "pipe.md", mode=0o600)
    _write(private_root, "note.md")
    with _source(private_root) as source:
        baseline = open_descriptors()
        for _ in range(20):
            for name in ("folder.md", "pipe.md"):
                with pytest.raises(NoteUnavailable):
                    source.read(name, ReadBudget(100))
            source.read("note.md", ReadBudget(100))
        assert open_descriptors() == baseline


@pytest.mark.parametrize("kind", [stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK])
def test_opened_nonregular_metadata_is_refused_without_reading(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, kind: int
) -> None:
    target = _write(private_root, "device.md").stat().st_ino
    actual = os.fstat

    def nonregular(descriptor: int) -> os.stat_result:
        info = actual(descriptor)
        return _changed_info(info, stat.ST_MODE, kind | 0o600) if info.st_ino == target else info

    monkeypatch.setattr(filesystem.os, "fstat", nonregular)
    budget = ReadBudget(100)
    with _source(private_root) as source, pytest.raises(NoteUnavailable, match="unsafe_file"):
        source.read("device.md", budget)
    assert budget.consumed == 0


@pytest.mark.parametrize(
    "filename",
    [
        "../outside.md",
        "/outside.md",
        "nested/note.md",
        r"..\outside.md",
        "./note.md",
        ".",
        "..",
        ".hidden.md",
        "https://example.invalid/note.md",
        "C:\\private\\note.md",
        "note.md\x00",
        "line\nbreak.md",
        "note\x7f.md",
        "note\udcff.md",
        "x" * 256 + ".md",
        "note.pdf",
        "note.docx",
        "note.zip",
        "note.html",
    ],
)
def test_paths_hidden_files_and_unsupported_formats_are_refused(private_root: Path, filename: str) -> None:
    with _source(private_root) as source, pytest.raises(ToolError, match="filename"):
        source.read(filename, ReadBudget(100))


def test_filename_limit_counts_encoded_bytes_not_only_characters() -> None:
    assert supported_name("\u00e9" * 126 + ".md", (".md",))
    assert not supported_name("\u00e9" * 127 + ".md", (".md",))
    assert supported_name("Mixed Case.MD", (".md",))


def test_listing_does_not_descend_into_nested_folders(private_root: Path) -> None:
    nested = private_root / "nested"
    nested.mkdir(mode=0o700)
    _write(nested, "hidden-from-scope.md", b"not part of this collection")
    _write(private_root, "top.md")
    with _source(private_root) as source:
        notes, skipped, examined = source.candidates()
        assert [item.name for item in notes] == ["top.md"]
        assert examined == 2
        assert skipped["unsupported_name"] == 1


def test_declared_extensions_are_enforced(private_root: Path) -> None:
    _write(private_root, "note.md")
    _write(private_root, "note.txt")
    settings = load_settings(
        {"collections": [{"name": "notes", "path": str(private_root), "extensions": [".md"]}]}
    )
    with open_collection(settings.collections[0], settings, lambda: None) as source:
        assert [item.name for item in source.candidates()[0]] == ["note.md"]
        with pytest.raises(ToolError, match="filename"):
            source.read("note.txt", ReadBudget(100))


def test_entry_limit_counts_hidden_unsupported_and_directory_entries(private_root: Path) -> None:
    _write(private_root, "note.md")
    _write(private_root, ".hidden")
    (private_root / "nested").mkdir(mode=0o700)
    with _source(private_root, max_entries=2) as source, pytest.raises(ToolError, match="entry limit"):
        source.candidates()


def test_enumeration_stops_at_one_entry_beyond_the_limit(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yielded: list[int] = []

    class Entries:
        def __enter__(self) -> Iterator[SimpleNamespace]:
            def names() -> Iterator[SimpleNamespace]:
                for index in range(10_000):
                    yielded.append(index)
                    yield SimpleNamespace(name=f".hidden-{index}")

            return names()

        def __exit__(self, *_args: Any) -> None:
            pass

    with _source(private_root, max_entries=2) as source:
        monkeypatch.setattr(filesystem.os, "scandir", lambda _descriptor: Entries())
        with pytest.raises(ToolError, match="entry limit"):
            source.candidates()
    assert yielded == [0, 1, 2]


def test_actual_files_at_and_above_configured_byte_limit(private_root: Path) -> None:
    _write(private_root, "exact.md", b"x" * 16)
    _write(private_root, "large.md", b"x" * 17)
    with _source(private_root, max_file_bytes=16) as source:
        assert source.read("exact.md", ReadBudget(17)).size_bytes == 16
        budget = ReadBudget(100)
        with pytest.raises(NoteUnavailable, match="too_large"):
            source.read("large.md", budget)
        assert budget.consumed == 0
        notes, skipped, _ = source.candidates()
        assert [item.name for item in notes] == ["exact.md"]
        assert skipped["too_large"] == 1


def test_real_read_bytes_are_bounded_even_when_metadata_underreports_size(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write(private_root, "growing.md", b"x" * 100).stat().st_ino
    actual = os.fstat

    def small_size(descriptor: int) -> os.stat_result:
        info = actual(descriptor)
        return _changed_info(info, stat.ST_SIZE, 1) if info.st_ino == target else info

    monkeypatch.setattr(filesystem.os, "fstat", small_size)
    budget = ReadBudget(1000)
    with (
        _source(private_root, max_file_bytes=16) as source,
        pytest.raises(NoteUnavailable, match="too_large"),
    ):
        source.read("growing.md", budget)
    assert budget.consumed == 17


def test_aggregate_byte_budget_is_shared_and_reserves_an_end_check(private_root: Path) -> None:
    for name in ("a.md", "b.md", "c.md"):
        _write(private_root, name, b"four")
    with _source(private_root) as source:
        budget = ReadBudget(9)
        source.read("a.md", budget)
        source.read("b.md", budget)
        with pytest.raises(SearchBudgetExhausted):
            source.read("c.md", budget)
        assert (budget.consumed, budget.remaining) == (8, 1)
        exact = ReadBudget(8)
        source.read("a.md", exact)
        with pytest.raises(SearchBudgetExhausted):
            source.read("b.md", exact)
        assert exact.consumed == 4


def test_actual_read_cannot_exceed_aggregate_budget_after_growth(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write(private_root, "growing.md", b"x" * 100).stat().st_ino
    actual = os.fstat

    def small_size(descriptor: int) -> os.stat_result:
        info = actual(descriptor)
        return _changed_info(info, stat.ST_SIZE, 1) if info.st_ino == target else info

    monkeypatch.setattr(filesystem.os, "fstat", small_size)
    budget = ReadBudget(7)
    with _source(private_root, max_file_bytes=32) as source, pytest.raises(SearchBudgetExhausted):
        source.read("growing.md", budget)
    assert (budget.consumed, budget.remaining) == (7, 0)


@pytest.mark.parametrize("raw", [b"\xff", b"before\x00after", b"\x1b[2J", b"bad\x7f", b"\xc2\x85"])
def test_invalid_encoding_and_binary_controls_are_refused(private_root: Path, raw: bytes) -> None:
    _write(private_root, "binary.md", raw)
    budget = ReadBudget(100)
    with _source(private_root) as source, pytest.raises(NoteUnavailable, match="invalid_text"):
        source.read("binary.md", budget)
    assert budget.consumed == len(raw)


def test_unicode_byte_order_mark_and_plain_whitespace_are_preserved(private_root: Path) -> None:
    text = "caf\u00e9\t\r\nsecond line\n"
    _write(private_root, "unicode.md", b"\xef\xbb\xbf" + text.encode("utf-8"))
    with _source(private_root) as source:
        assert source.read("unicode.md", ReadBudget(100)).text == text


def test_empty_notes_are_valid_and_descriptors_are_closed(private_root: Path) -> None:
    _write(private_root, "empty.md", b"")
    with _source(private_root) as source:
        descriptor = source.descriptor
        assert source.read("empty.md", ReadBudget(1)).text == ""
    with pytest.raises(OSError) as exc:
        os.fstat(descriptor)
    assert exc.value.errno == errno.EBADF


@pytest.mark.parametrize("change", ["delete", "edit", "permission", "replace"])
def test_changed_notes_invalidate_accumulated_results(private_root: Path, change: str) -> None:
    path = _write(private_root, "note.md")
    with pytest.raises(ToolError), _source(private_root) as source:
        source.read("note.md", ReadBudget(100))
        if change == "delete":
            path.unlink()
        elif change == "permission":
            path.chmod(0o644)
        elif change == "replace":
            replacement = _write(private_root, "replacement.md", b"replacement")
            replacement.replace(path)
        else:
            path.write_bytes(b"edited")


def test_root_revocation_invalidates_accumulated_results(private_root: Path) -> None:
    _write(private_root, "note.md")
    with pytest.raises(ToolError, match="collection unavailable"), _source(private_root) as source:
        source.read("note.md", ReadBudget(100))
        private_root.chmod(0o755)


def test_replaced_root_is_not_the_previously_opened_source(private_root: Path, tmp_path: Path) -> None:
    _write(private_root, "note.md")
    with pytest.raises(ToolError, match="collection changed"), _source(private_root) as source:
        source.read("note.md", ReadBudget(100))
        private_root.rename(tmp_path / "old-notes")
        private_root.mkdir(mode=0o700)
        _write(private_root, "note.md", b"new source")


def test_root_deletion_invalidates_even_an_empty_listing(private_root: Path) -> None:
    with pytest.raises(ToolError, match="collection unavailable"), _source(private_root) as source:
        assert source.candidates()[0] == []
        private_root.rmdir()


def test_missing_note_is_an_expected_safe_error(private_root: Path) -> None:
    with _source(private_root) as source, pytest.raises(NoteUnavailable, match="unavailable"):
        source.read("missing.md", ReadBudget(100))


def test_root_and_file_opens_are_read_only_and_never_follow_links(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(private_root, "note.md")
    actual = os.open
    opened: list[tuple[Any, int, Any]] = []

    def record(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append((path, flags, kwargs.get("dir_fd")))
        return actual(path, flags, *args, **kwargs)

    monkeypatch.setattr(filesystem.os, "open", record)
    with _source(private_root) as source:
        source.read("note.md", ReadBudget(100))
    assert opened
    assert all(flags & os.O_NOFOLLOW for _, flags, _ in opened)
    assert all(flags & os.O_ACCMODE == os.O_RDONLY for _, flags, _ in opened)
    assert all(not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND) for _, flags, _ in opened)
    note_flags = [flags for path, flags, directory in opened if path == "note.md" and directory is not None]
    assert len(note_flags) == 1
    assert note_flags[0] & os.O_NONBLOCK


@pytest.mark.parametrize("change", ["edit", "symlink", "delete", "permission"])
def test_changes_during_the_raw_read_are_refused_and_file_handles_close(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = b"original synthetic note"
    path = _write(private_root, "note.md", original)
    outside = _write(private_root.parent, "outside.md", b"never follow this source")
    actual = os.fdopen
    opened: list[BinaryIO] = []

    class Reader:
        def __init__(self, handle: BinaryIO) -> None:
            self.handle = handle

        def __enter__(self) -> Reader:
            return self

        def __exit__(self, *_args: Any) -> None:
            self.handle.close()

        def fileno(self) -> int:
            return self.handle.fileno()

        def read(self, maximum: int) -> bytes:
            raw = self.handle.read(maximum)
            if change == "permission":
                path.chmod(0o644)
            elif change == "edit":
                path.write_bytes(b"edited")
            else:
                path.unlink()
                if change == "symlink":
                    path.symlink_to(outside)
            return raw

    def racing_open(descriptor: int, mode: str, **options: Any) -> Reader:
        assert mode == "rb"
        handle = cast(BinaryIO, actual(descriptor, mode, **options))
        opened.append(handle)
        return Reader(handle)

    monkeypatch.setattr(filesystem.os, "fdopen", racing_open)
    budget = ReadBudget(100)
    with _source(private_root) as source, pytest.raises(NoteUnavailable):
        source.read("note.md", budget)
    assert budget.consumed == len(original)
    assert opened and all(handle.closed for handle in opened)


def test_unsupported_no_follow_platform_fails_closed(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(filesystem.os, "O_NOFOLLOW")
    with pytest.raises(ToolError, match="no-follow support"), _source(private_root):
        pass
