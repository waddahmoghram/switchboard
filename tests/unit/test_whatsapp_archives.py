"""Synthetic archives are selected in memory, never extracted or used for account access."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest

from switchboard.connectors._archive import ImportFormatError
from switchboard.connectors.whatsapp.service import MAX_MEMBER_NAME_CHARS, select_export_text

CHAT = b"11/10/26, 10:00 - Example Sender: synthetic text"


def _zip(entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


def test_plain_text_rejects_archive_selection() -> None:
    assert select_export_text(CHAT, zipped=False) == (CHAT, None)
    with pytest.raises(ImportFormatError, match="only supported for ZIP"):
        select_export_text(CHAT, zipped=False, archive_member="chat.txt")


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_one_nested_text_member_is_selected_and_media_remains_unparsed(compression: int) -> None:
    raw = _zip(
        [("folder/Chat.TXT", CHAT), ("photo.jpg", b"not an image decoder input")], compression=compression
    )
    assert select_export_text(raw, zipped=True) == (CHAT, "folder/Chat.TXT")


def test_multiple_text_members_require_explicit_selection() -> None:
    raw = _zip([("chats/first.txt", CHAT), ("chats/second.txt", b"not selected")])
    with pytest.raises(ImportFormatError, match="multiple text exports"):
        select_export_text(raw, zipped=True)
    assert select_export_text(raw, zipped=True, archive_member="chats/first.txt") == (CHAT, "chats/first.txt")
    with pytest.raises(ImportFormatError, match="does not match"):
        select_export_text(raw, zipped=True, archive_member="../../outside.txt")
    with pytest.raises(ImportFormatError, match="does not match"):
        select_export_text(raw, zipped=True, archive_member="missing.txt")


def test_automatic_selection_also_bounds_member_name_length() -> None:
    raw = _zip([("x" * MAX_MEMBER_NAME_CHARS + ".txt", CHAT)])
    with pytest.raises(ImportFormatError, match="at most 1024 characters"):
        select_export_text(raw, zipped=True)


@pytest.mark.parametrize("unsafe", ["../photo.jpg", "/photo.jpg", r"C:\photo.jpg", r"..\photo.jpg"])
def test_selection_does_not_bypass_unsafe_ignored_media_members(unsafe: str) -> None:
    raw = _zip([("chat.txt", CHAT), (unsafe, b"ignored media")])
    with pytest.raises(ImportFormatError, match="unsafe"):
        select_export_text(raw, zipped=True, archive_member="chat.txt")


def test_selection_does_not_bypass_archived_symbolic_links() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("chat.txt", CHAT)
        member = zipfile.ZipInfo("photo.jpg")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "outside.jpg")
    with pytest.raises(ImportFormatError, match="symbolic link"):
        select_export_text(output.getvalue(), zipped=True, archive_member="chat.txt")


def test_duplicate_text_names_and_archives_without_text_are_refused() -> None:
    with pytest.raises(ImportFormatError, match="duplicate"):
        select_export_text(_zip([("CHAT.txt", CHAT), ("chat.TXT", CHAT)]), zipped=True)
    with pytest.raises(ImportFormatError, match="no supported text"):
        select_export_text(_zip([("photo.jpg", b"ignored")]), zipped=True)
    with pytest.raises(ImportFormatError, match="cannot be read safely"):
        select_export_text(b"not an archive", zipped=True)
