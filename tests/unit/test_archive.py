"""Private export boundaries and transactional snapshot storage; these tests use synthetic files."""

from __future__ import annotations

import io
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from switchboard.connectors import _archive
from switchboard.connectors._archive import (
    ArchiveRecord,
    ArchiveStore,
    ImportFormatError,
    archive_migrations,
    read_import_file,
    zip_members,
)
from switchboard.db import transaction
from switchboard.errors import ToolError
from tests.conftest import AppFactory


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


def test_private_regular_files_are_read_by_basename(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "export.csv").write_bytes(b"header\nvalue\n")
    (tmp_path / "export.csv").chmod(0o600)
    assert read_import_file(tmp_path, "export.csv", (".csv",)) == b"header\nvalue\n"


@pytest.mark.parametrize("filename", ["../export.csv", "/export.csv", r"..\export.csv", "export.exe"])
def test_paths_and_unsupported_file_types_are_refused(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ToolError, match="filename"):
        read_import_file(tmp_path, filename, (".csv",))


def test_symlinks_and_shared_import_directories_are_refused(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "real.csv").write_bytes(b"private")
    (tmp_path / "real.csv").chmod(0o600)
    (tmp_path / "linked.csv").symlink_to(tmp_path / "real.csv")
    with pytest.raises(ToolError, match="symbolic links"):
        read_import_file(tmp_path, "linked.csv", (".csv",))
    tmp_path.chmod(0o755)
    with pytest.raises(ToolError, match="mode 700"):
        read_import_file(tmp_path, "real.csv", (".csv",))


def test_a_directory_with_an_export_name_is_refused_and_closed(
    tmp_path: Path, open_descriptors: Callable[[], int]
) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "export.csv").mkdir()
    baseline = open_descriptors()
    with pytest.raises(ToolError, match="owned regular file"):
        read_import_file(tmp_path, "export.csv", (".csv",))
    assert open_descriptors() == baseline


def test_actual_file_and_inflated_archive_sizes_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "large.txt").write_bytes(b"x" * 17)
    (tmp_path / "large.txt").chmod(0o600)
    monkeypatch.setattr(_archive, "MAX_IMPORT_BYTES", 16)
    with pytest.raises(ImportFormatError):
        read_import_file(tmp_path, "large.txt", (".txt",))
    raw = _zip([("first.txt", b"a" * 200), ("second.txt", b"b" * 200)])
    limit = len(raw) + 1
    assert 200 < limit < 400
    monkeypatch.setattr(_archive, "MAX_IMPORT_BYTES", limit)
    with pytest.raises(ImportFormatError, match="uncompressed text limit"):
        zip_members(raw, (".txt",))
    monkeypatch.setattr(_archive, "MAX_ARCHIVE_MEMBERS", 1)
    with pytest.raises(ImportFormatError, match="entries"):
        zip_members(raw, (".txt",))


@pytest.mark.parametrize("name", ["../messages.csv", "/messages.csv", r"C:\messages.csv"])
def test_unsafe_archive_members_are_not_extracted_or_read(name: str) -> None:
    with pytest.raises(ImportFormatError, match="unsafe"):
        zip_members(_zip([(name, b"content")]), (".csv",))


def test_archived_symbolic_links_are_refused() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        member = zipfile.ZipInfo("messages.csv")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "outside.csv")
    with pytest.raises(ImportFormatError, match="symbolic link"):
        zip_members(output.getvalue(), (".csv",))


def test_members_are_read_without_creating_files() -> None:
    [member] = zip_members(_zip([("folder/messages.csv", b"a,b\n1,2\n"), ("photo.jpg", b"image")]), (".csv",))
    assert member.name == "folder/messages.csv"
    assert member.data == b"a,b\n1,2\n"


def test_snapshot_imports_are_idempotent_and_preserve_identical_repeated_rows(
    make_app: AppFactory,
) -> None:
    app = make_app()
    app.ctx.db.add_migrations("example", archive_migrations("example"))
    app.ctx.db.migrate()
    store = ArchiveStore(app.ctx, "example")
    records = [ArchiveRecord("message", "Example", {"text": "same message"})] * 2
    result = store.import_records("first", "export.csv", records)
    assert result["inserted"] == 2
    assert store.import_records("first", "renamed.csv", records)["status"] == "duplicate"
    assert store.import_records("overlap", "new-export.csv", records)["inserted"] == 0
    found = store.search("same message")
    assert len(found["records"]) == 2
    assert found["historical"] is True
    assert all(item["untrusted_content"] for item in found["records"])
    assert store.read(found["records"][0]["id"])["data"]["text"] == "same message"
    assert [event.type for event in app.ctx.events.recent()] == ["example.imported", "example.imported"]


def test_search_uses_literal_text_not_wildcard_expressions(make_app: AppFactory) -> None:
    app = make_app()
    app.ctx.db.add_migrations("example", archive_migrations("example"))
    app.ctx.db.migrate()
    store = ArchiveStore(app.ctx, "example")
    store.import_records("one", "one.txt", [ArchiveRecord("message", "Example", {"text": "only 10%"})])
    assert len(store.search("%")["records"]) == 1
    assert store.search("_")["records"] == []


def test_event_insertion_can_be_rolled_back_with_its_source_state(make_app: AppFactory) -> None:
    app = make_app()

    def fail_after_writing() -> None:
        with app.ctx.db.session() as conn, transaction(conn):
            app.ctx.events.emit("example", "example.new", {}, dedupe_key="atomic", connection=conn)
            conn.execute(
                "INSERT INTO kv (namespace, key, value, updated_at) VALUES ('example', 'cursor', '1', 'now')"
            )
            raise RuntimeError("simulate a failed source update")

    with pytest.raises(RuntimeError, match="simulate a failed source update"):
        fail_after_writing()
    assert app.ctx.events.recent() == []
    assert app.ctx.db.kv_get("example", "cursor") is None


def test_failed_summary_enqueue_rolls_back_records_and_import_marker(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    app.ctx.db.add_migrations("example", archive_migrations("example"))
    app.ctx.db.migrate()
    store = ArchiveStore(app.ctx, "example")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(app.ctx.events, "emit", fail)
    with pytest.raises(RuntimeError, match="event failure"):
        store.import_records("one", "one.txt", [ArchiveRecord("message", "Example", {"text": "hello"})])
    with app.ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM example_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM example_imports").fetchone()[0] == 0
