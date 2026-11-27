"""The three opt-in integrations assemble offline and expose the agreed assistant tool surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard.mcp_server import McpServer
from switchboard.policy import Tier
from tests.conftest import AppFactory
from tests.fakes import FakeHttp
from tests.fixtures.telegram import TOKEN, config

pytestmark = pytest.mark.integration


def test_social_connectors_register_and_list_tools_without_account_calls(
    make_app: AppFactory, fake_http: FakeHttp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SB_TEST_TELEGRAM_BOT", TOKEN)
    linkedin = tmp_path / "linkedin"
    whatsapp = tmp_path / "whatsapp"
    linkedin.mkdir(mode=0o700)
    whatsapp.mkdir(mode=0o700)
    settings = config()
    settings["switchboard"]["connectors"] = ["telegram", "linkedin", "whatsapp"]
    settings["connectors"]["linkedin"] = {
        "import_dir": str(linkedin),
        "publishing_enabled": False,
        "author_urn": "",
        "api_version": "202610",
    }
    settings["connectors"]["whatsapp"] = {
        "import_dir": str(whatsapp),
        "date_order": "MDY",
        "timezone": "America/Los_Angeles",
    }
    app = make_app(settings)
    server = McpServer(tools=app.registry.all, call=app.call)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
    )
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert {
        "switchboard_status",
        "approvals_pending",
        "approvals_status",
        "actions_recent",
        "telegram_status",
        "telegram_messages",
        "telegram_read",
        "telegram_mark_read",
        "telegram_reply",
        "linkedin_import_export",
        "linkedin_search",
        "linkedin_read",
        "linkedin_draft_post",
        "linkedin_drafts",
        "linkedin_publish_post",
        "whatsapp_import_export",
        "whatsapp_search",
        "whatsapp_read",
    } == names
    publisher = app.registry.get("linkedin_publish_post")
    assert publisher is not None
    assert publisher.tier is Tier.ACT
    assert fake_http.requests == []
