"""Import identity includes parsing context; overlapping synthetic messages keep multiplicity."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import switchboard.connectors.whatsapp.service as whatsapp_service
from switchboard.connectors.whatsapp import MIGRATIONS, doctor, register
from switchboard.connectors.whatsapp.service import WhatsAppArchive
from switchboard.connectors.whatsapp.settings import Settings
from switchboard.errors import ToolError
from switchboard.policy import Tier
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

CHAT = b"11/10/26, 10:00 - Example Sender: same message\n"


def _private_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "exports"
    inbox.mkdir(mode=0o700)
    inbox.chmod(0o700)
    return inbox


def _write(directory: Path, name: str, raw: bytes) -> None:
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)


def test_contextual_repeated_and_overlapping_imports(make_app: AppFactory, tmp_path: Path) -> None:
    inbox = _private_inbox(tmp_path)
    app = make_app()
    app.ctx.db.add_migrations("whatsapp", MIGRATIONS)
    app.ctx.db.migrate()
    runtime = WhatsAppArchive(app.ctx, Settings(inbox, "MDY"))
    _write(inbox, "first.txt", CHAT * 2)
    _write(inbox, "renamed.txt", CHAT * 2)
    args = {"filename": "first.txt", "chat_label": "Example chat"}
    assert runtime.import_export(args)["inserted"] == 2
    assert runtime.import_export(args)["status"] == "duplicate"
    assert runtime.import_export({**args, "filename": "renamed.txt"})["status"] == "duplicate"
    _write(inbox, "overlap.txt", CHAT * 3)
    result = runtime.import_export({**args, "filename": "overlap.txt"})
    assert result["records"] == 3
    assert result["inserted"] == 1
    assert len(runtime.search({})["records"]) == 3
    assert runtime.import_export({**args, "chat_label": "Other human label"})["inserted"] == 2
    assert len(runtime.search({"chat_label": "Example chat"})["records"]) == 3
    assert len(runtime.search({"chat_label": "Other human label"})["records"]) == 2
    different_zone = WhatsAppArchive(app.ctx, Settings(inbox, "MDY", "America/Los_Angeles"))
    zoned = different_zone.import_export(args)
    assert zoned["status"] == "imported"
    assert zoned["inserted"] == 2
    assert different_zone.import_export(args)["inserted"] == 0
    different_order = WhatsAppArchive(app.ctx, Settings(inbox, "DMY"))
    assert different_order.import_export(args)["inserted"] == 2
    assert len(app.ctx.events.recent(100)) == 5


def test_equivalent_records_keep_distinct_import_markers_for_changed_parsing_context(
    make_app: AppFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _private_inbox(tmp_path)
    _write(inbox, "chat.txt", b"11/11/26, 10:00 - Example Sender: same message")
    app = make_app()
    app.ctx.db.add_migrations("whatsapp", MIGRATIONS)
    app.ctx.db.migrate()
    args = {"filename": "chat.txt", "chat_label": "Example chat"}
    first = WhatsAppArchive(app.ctx, Settings(inbox, "MDY")).import_export(args)
    second = WhatsAppArchive(app.ctx, Settings(inbox, "DMY")).import_export(args)
    assert first["inserted"] == 1
    assert second["status"] == "imported"
    assert second["inserted"] == 0
    monkeypatch.setattr(whatsapp_service, "PARSER_VERSION", "numeric-test-version")
    third = WhatsAppArchive(app.ctx, Settings(inbox, "MDY")).import_export(args)
    assert third["status"] == "imported"
    assert third["inserted"] == 0
    assert len(app.ctx.events.recent()) == 3


def test_archive_member_is_part_of_import_identity(make_app: AppFactory, tmp_path: Path) -> None:
    inbox = _private_inbox(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("first.txt", CHAT)
        archive.writestr("second.txt", CHAT)
    _write(inbox, "export.zip", output.getvalue())
    app = make_app()
    app.ctx.db.add_migrations("whatsapp", MIGRATIONS)
    app.ctx.db.migrate()
    runtime = WhatsAppArchive(app.ctx, Settings(inbox, "MDY"))
    args = {"filename": "export.zip", "chat_label": "Example chat"}
    first = runtime.import_export({**args, "archive_member": "first.txt"})
    second = runtime.import_export({**args, "archive_member": "second.txt"})
    assert first["inserted"] == 1
    assert second["status"] == "imported"
    assert second["inserted"] == 0
    assert second["archive_member"] == "second.txt"
    assert sorted(path.name for path in inbox.iterdir()) == ["export.zip"]


def test_registration_is_offline_and_adds_only_three_tools(
    make_app: AppFactory, tmp_path: Path, fake_http: FakeHttp
) -> None:
    inbox = tmp_path / "not-created"
    app = make_app({"connectors": {"whatsapp": {"date_order": "MDY", "import_dir": str(inbox)}}})
    before_jobs = app.ctx.scheduler.jobs()
    register(app.ctx, app.registry)
    tools = {tool.name: tool.tier for tool in app.registry.all() if tool.connector == "whatsapp"}
    assert tools == {
        "whatsapp_import_export": Tier.DRAFT,
        "whatsapp_search": Tier.READ,
        "whatsapp_read": Tier.READ,
    }
    assert app.ctx.scheduler.jobs() == before_jobs
    assert not inbox.exists()
    assert fake_http.requests == []
    [problem] = doctor(app.ctx)
    assert problem.startswith("cannot inspect the import folder")


def test_doctor_checks_folder_permissions_without_importing(
    make_app: AppFactory, tmp_path: Path, fake_http: FakeHttp
) -> None:
    inbox = _private_inbox(tmp_path)
    _write(inbox, "unsupported.txt", b"not a chat")
    app = make_app({"connectors": {"whatsapp": {"date_order": "MDY", "import_dir": str(inbox)}}})
    assert doctor(app.ctx) == []
    inbox.chmod(0o755)
    assert "mode 700" in doctor(app.ctx)[0]
    assert app.ctx.events.recent() == []
    assert fake_http.requests == []


@pytest.mark.parametrize("label", ["", " \t", "Example\nchat", "x" * 201])
def test_human_label_is_required_and_bounded(make_app: AppFactory, label: str) -> None:
    app = make_app()
    runtime = WhatsAppArchive(app.ctx, Settings(None, "MDY"))
    with pytest.raises(ToolError, match="chat_label"):
        runtime.import_export({"filename": "chat.txt", "chat_label": label})
