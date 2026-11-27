"""Cross-connector offline registration and assistant exposure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from switchboard.mcp_server import META_PROTOCOL_VERSION, MODERN_VERSIONS, McpServer
from switchboard.policy import Origin
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

pytestmark = pytest.mark.integration


def _settings(tmp_path: Path) -> dict[str, Any]:
    notes = tmp_path / "personal-notes"
    notes.mkdir(mode=0o700)
    return {
        "switchboard": {"connectors": ["documents", "tasks", "shelly"]},
        "connectors": {
            "documents": {
                "collections": [{"name": "personal", "path": str(notes), "extensions": [".md", ".txt"]}],
            },
            "tasks": {},
            "shelly": {
                "timeout_seconds": 5,
                "devices": {
                    "desk_lamp": {
                        "base_url": "http://192.168.10.40",
                        "allow_http": True,
                        "expected_device_id": "shellyplusplugs-000000000001",
                        "expected_model": "SNPL-00112EU",
                        "expected_generation": 2,
                        "switch_ids": [0],
                        "password": "env:SB_TEST_SHELLY_PASSWORD",
                        "allow_control": False,
                    },
                },
            },
        },
    }


def test_local_documents_tasks_and_disabled_device_controls_register_offline(
    make_app: AppFactory, fake_http: FakeHttp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_SHELLY_PASSWORD", raising=False)
    app = make_app(_settings(tmp_path))
    assert {tool.name for tool in app.registry.all()} == {
        "switchboard_status",
        "approvals_pending",
        "approvals_status",
        "actions_recent",
        "documents_collections",
        "documents_list",
        "documents_search",
        "documents_read",
        "tasks_status",
        "tasks_list",
        "tasks_read",
        "tasks_add",
        "tasks_update",
        "tasks_delete",
        "shelly_devices",
        "shelly_status",
    }
    assert app.call("shelly_devices", {}, Origin.ASSISTANT).status == "ok"
    assert app.call("tasks_status", {}, Origin.ASSISTANT).data["last_check"] is None
    assert app.ctx.events.pending() == []
    assert fake_http.requests == []


def test_assistant_exposure_list_does_not_make_unexposed_mutations_callable(
    make_app: AppFactory, fake_http: FakeHttp, tmp_path: Path
) -> None:
    app = make_app(_settings(tmp_path))
    patterns = ["documents_*", "tasks_status", "tasks_list", "tasks_read"]
    server = McpServer(tools=lambda: app.registry.exposed(patterns), call=app.call)
    listed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {META_PROTOCOL_VERSION: MODERN_VERSIONS[0]}},
        }
    )
    assert listed is not None
    assert len(listed["result"]["tools"]) == 7
    rejected = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "_meta": {META_PROTOCOL_VERSION: MODERN_VERSIONS[0]},
                "name": "tasks_delete",
                "arguments": {"id": 1, "revision": 1},
            },
        }
    )
    assert rejected is not None
    assert rejected["error"]["code"] == -32602
    assert app.approvals.list("pending") == []
    assert fake_http.requests == []


def test_native_lighting_metadata_and_action_schemas_do_not_contact_devices(
    make_app: AppFactory, fake_http: FakeHttp
) -> None:
    app = make_app(
        {
            "switchboard": {"connectors": ["wled"]},
            "connectors": {
                "wled": {
                    "timeout_seconds": 5,
                    "max_response_bytes": 65536,
                    "devices": [
                        {
                            "name": "desk",
                            "base_url": "http://192.168.50.20",
                            "allow_http": True,
                            "allow_global": False,
                            "segments": [0],
                            "presets": [],
                        }
                    ],
                }
            },
        }
    )
    assert app.call("wled_devices", {}, Origin.ASSISTANT).status == "ok"
    assert {tool.name for tool in app.registry.all()} == {
        "switchboard_status",
        "approvals_pending",
        "approvals_status",
        "actions_recent",
        "wled_devices",
        "wled_status",
        "wled_set_power",
        "wled_set_brightness",
        "wled_set_color",
        "wled_set_transition",
        "wled_apply_preset",
    }
    assert app.ctx.events.pending() == []
    assert fake_http.requests == []


def test_monitoring_status_is_cached_and_does_not_probe_health_on_registration(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_HEALTH_KEY", raising=False)
    app = make_app(
        {
            "switchboard": {"connectors": ["monitoring"]},
            "connectors": {
                "monitoring": {
                    "checks": {
                        "personal_service": {
                            "kind": "endpoint",
                            "url": "https://health.example.invalid/ready",
                            "readiness_field": "status",
                            "readiness_equals": "ready",
                            "bearer_token": "env:SB_TEST_HEALTH_KEY",
                        }
                    },
                }
            },
        }
    )
    assert app.call("monitoring_status", {}, Origin.ASSISTANT).status == "ok"
    assert app.ctx.events.pending() == []
    assert fake_http.requests == []


def test_all_six_local_families_assemble_without_device_or_account_calls(
    make_app: AppFactory, fake_http: FakeHttp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_HOME_TOKEN", raising=False)
    monkeypatch.delenv("SB_TEST_SHELLY_PASSWORD", raising=False)
    settings = _settings(tmp_path)
    settings["switchboard"]["connectors"] += ["home", "wled", "monitoring"]
    settings["connectors"]["home"] = {
        "base_url": "https://homeassistant.example.invalid:8123",
        "token": "env:SB_TEST_HOME_TOKEN",
        "expose": ["light.example"],
    }
    settings["connectors"]["wled"] = {
        "devices": [
            {
                "name": "desk",
                "base_url": "http://192.168.50.20",
                "allow_http": True,
                "allow_global": False,
                "segments": [0],
                "presets": [],
            }
        ],
    }
    settings["connectors"]["monitoring"] = {
        "checks": {"personal_service": {"kind": "endpoint", "url": "https://health.example.invalid/"}},
    }
    app = make_app(settings)
    assert sorted(app.connectors) == ["documents", "home", "monitoring", "shelly", "tasks", "wled"]
    names = {tool.name for tool in app.registry.all()}
    assert {
        "home_states",
        "home_call_service",
        "wled_devices",
        "wled_set_power",
        "shelly_devices",
        "shelly_status",
        "documents_collections",
        "documents_search",
        "tasks_list",
        "tasks_add",
        "tasks_delete",
        "monitoring_status",
        "monitoring_check",
    } <= names
    assert app.call("switchboard_status", {}, Origin.ASSISTANT).data["tools"] == len(names)
    assert app.ctx.events.pending() == []
    assert fake_http.requests == []


def test_minimal_home_configuration_keeps_new_profiles_polling_and_speech_inert(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SB_TEST_HOME_TOKEN", raising=False)
    app = make_app(
        {
            "switchboard": {"connectors": ["home"]},
            "connectors": {
                "home": {
                    "base_url": "https://homeassistant.example.invalid:8123",
                    "token": "env:SB_TEST_HOME_TOKEN",
                    "expose": ["light.example"],
                }
            },
        }
    )
    assert {tool.name for tool in app.registry.all()} == {
        "switchboard_status",
        "approvals_pending",
        "approvals_status",
        "actions_recent",
        "home_states",
        "home_targets",
        "home_sensor_summary",
        "home_poll_status",
        "home_poll",
        "home_call_service",
        "home_lights",
        "home_climate",
        "home_fan",
        "home_media",
        "home_lighting_preset",
    }
    assert "home.poll" not in {job.name for job in app.ctx.scheduler.jobs()}
    assert app.call("home_targets", {}, Origin.ASSISTANT).status == "ok"
    assert app.call("home_poll", {}, Origin.ASSISTANT).status == "error"
    assert fake_http.requests == []
