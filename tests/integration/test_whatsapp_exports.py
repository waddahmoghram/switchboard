"""Offline tool, storage, and summary-event flow with only synthetic private folders."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.policy import Origin, Tier
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "whatsapp"


def _config(inbox: Path, *, date_order: str = "MDY") -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["whatsapp"]},
        "connectors": {
            "whatsapp": {
                "import_dir": str(inbox),
                "date_order": date_order,
                "timezone": "America/Los_Angeles",
            }
        },
    }


def _private_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "exports"
    inbox.mkdir(mode=0o700)
    inbox.chmod(0o700)
    return inbox


def _write(inbox: Path, name: str, raw: bytes) -> None:
    path = inbox / name
    path.write_bytes(raw)
    path.chmod(0o600)


def _call(app: App, tool: str, args: dict[str, Any] | None = None) -> Any:
    outcome = app.call(tool, args or {}, Origin.ASSISTANT)
    assert outcome.status == "ok", outcome.message
    return outcome.data


def test_import_search_read_repeats_overlap_and_summary_only(
    make_app: AppFactory, tmp_path: Path, fake_http: FakeHttp
) -> None:
    inbox = _private_inbox(tmp_path)
    raw = (FIXTURES / "android_mdy.txt").read_bytes()
    _write(inbox, "chat.txt", raw)
    _write(inbox, "renamed.txt", raw)
    app = make_app(_config(inbox))
    expected_tiers = {
        "whatsapp_import_export": Tier.DRAFT,
        "whatsapp_search": Tier.READ,
        "whatsapp_read": Tier.READ,
    }
    assert {
        tool.name: tool.tier for tool in app.registry.all() if tool.connector == "whatsapp"
    } == expected_tiers
    args = {"filename": "chat.txt", "chat_label": "Example chat"}
    imported = _call(app, "whatsapp_import_export", args)
    assert (imported["records"], imported["inserted"]) == (4, 4)
    assert imported["historical"] is imported["untrusted_content"] is True
    assert imported["truncated_records"] == 0
    found = _call(app, "whatsapp_search", {"query": "first line", "chat_label": "Example chat"})
    [item] = found["records"]
    assert found["historical"] is found["untrusted_content"] is True
    assert item["preview_only"] is True
    record = _call(app, "whatsapp_read", {"id": item["id"]})
    assert record["historical"] is record["untrusted_content"] is True
    assert record["data"]["text"].startswith("First line.\nSecond line")
    assert record["data"]["timestamp_local"] == "2026-11-10T08:16:00"
    assert record["data"]["timestamp_has_offset"] is False
    assert record["data"]["timezone_context"] == "America/Los_Angeles"
    assert len(_call(app, "whatsapp_search", {"kind": "system_notice"})["records"]) == 1
    assert _call(app, "whatsapp_import_export", args)["status"] == "duplicate"
    assert _call(app, "whatsapp_import_export", {**args, "filename": "renamed.txt"})["status"] == "duplicate"
    _write(inbox, "overlap.txt", raw + b"11/10/26, 08:17 - Example Sender: Added later.\n")
    overlap = _call(app, "whatsapp_import_export", {**args, "filename": "overlap.txt"})
    assert (overlap["records"], overlap["inserted"]) == (5, 1)
    assert len(_call(app, "whatsapp_search", {"query": "Repeated message."})["records"]) == 2
    assert len(_call(app, "whatsapp_search")["records"]) == 5
    events = app.ctx.events.recent(100)
    assert [event.type for event in events] == ["whatsapp.imported", "whatsapp.imported"]
    assert all(event.source == "whatsapp" for event in events)
    payload = next(event.payload for event in events if event.payload["file"] == "chat.txt")
    assert set(payload) == {
        "parser",
        "source_sha256",
        "chat_label",
        "date_order",
        "timezone_context",
        "archive_member",
        "truncated_records",
        "untrusted_content",
        "file",
        "records",
        "inserted",
        "historical",
    }
    assert payload["records"] == payload["inserted"] == 4
    assert payload["historical"] is True
    assert payload["date_order"] == "MDY"
    assert not any(key in payload for key in ("text", "sender", "timestamp_local"))
    assert not any(job.name.startswith("whatsapp.") for job in app.ctx.scheduler.jobs())
    assert fake_http.requests == []


def test_archive_ambiguity_selection_and_no_media_extraction(
    make_app: AppFactory, tmp_path: Path, fake_http: FakeHttp
) -> None:
    inbox = _private_inbox(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("chats/selected.txt", (FIXTURES / "iphone_dmy.txt").read_bytes())
        archive.writestr("chats/other.txt", "not selected or parsed")
        archive.writestr("media/photo.jpg", "synthetic ignored media")
    _write(inbox, "export.zip", output.getvalue())
    app = make_app(_config(inbox, date_order="DMY"))
    args = {"filename": "export.zip", "chat_label": "Example chat"}
    ambiguous = app.call("whatsapp_import_export", args, Origin.ASSISTANT)
    assert ambiguous.status == "error"
    assert "multiple text exports" in ambiguous.message
    assert _call(app, "whatsapp_search")["records"] == []
    assert app.ctx.events.recent() == []
    result = _call(app, "whatsapp_import_export", {**args, "archive_member": "chats/selected.txt"})
    assert (result["records"], result["inserted"]) == (4, 4)
    assert result["archive_member"] == "chats/selected.txt"
    assert sorted(path.name for path in inbox.iterdir()) == ["export.zip"]
    assert fake_http.requests == []


def test_bad_second_record_is_atomic_and_does_not_echo_private_text(
    make_app: AppFactory, tmp_path: Path, fake_http: FakeHttp
) -> None:
    inbox = _private_inbox(tmp_path)
    _write(
        inbox,
        "bad.txt",
        b"11/10/26, 08:16 - Example Sender: valid\n2/30/26, 08:17 - Example Sender: PRIVATE_SYNTHETIC_BODY\n",
    )
    app = make_app(_config(inbox))
    outcome = app.call(
        "whatsapp_import_export", {"filename": "bad.txt", "chat_label": "Example chat"}, Origin.ASSISTANT
    )
    assert outcome.status == "error"
    assert "line 2" in outcome.message
    assert "PRIVATE_SYNTHETIC_BODY" not in outcome.message
    assert _call(app, "whatsapp_search")["records"] == []
    assert app.ctx.events.recent() == []
    with app.ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM whatsapp_imports").fetchone()[0] == 0
    assert fake_http.requests == []


@pytest.mark.parametrize("filename", ["../chat.txt", "/chat.txt", r"..\chat.txt", "chat.exe"])
def test_import_tool_cannot_read_arbitrary_paths(make_app: AppFactory, tmp_path: Path, filename: str) -> None:
    inbox = _private_inbox(tmp_path)
    app = make_app(_config(inbox))
    outcome = app.call(
        "whatsapp_import_export", {"filename": filename, "chat_label": "Example chat"}, Origin.ASSISTANT
    )
    assert outcome.status == "error"
    assert "filename, not a path" in outcome.message


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("whatsapp_import_export", {"filename": "chat.txt"}),
        ("whatsapp_import_export", {"filename": "chat.txt", "chat_label": "Example", "send": True}),
        ("whatsapp_search", {"limit": 0}),
        ("whatsapp_search", {"limit": 101}),
        ("whatsapp_search", {"query": "x" * 1001}),
        ("whatsapp_read", {"id": 0}),
        ("whatsapp_read", {"id": 2**63}),
    ],
)
def test_tool_schemas_bound_arguments(
    make_app: AppFactory, tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    inbox = _private_inbox(tmp_path)
    app = make_app(_config(inbox))
    assert app.call(tool, args, Origin.ASSISTANT).status == "invalid"
