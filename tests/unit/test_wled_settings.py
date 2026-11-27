"""Synthetic WLED configuration cases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors import wled
from switchboard.connectors.wled.settings import load_settings
from switchboard.context import AppContext
from switchboard.errors import ConfigError
from switchboard.policy import Tier
from tests.fakes import FakeHttp

AppFactory = Callable[..., App]


def settings(**overrides: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "devices": [
            {
                "name": "desk",
                "base_url": "http://192.168.50.20",
                "allow_http": True,
                "segments": [0, 1],
            }
        ]
    }
    table.update(overrides)
    return table


def config(table: dict[str, Any]) -> dict[str, Any]:
    return {"switchboard": {"connectors": ["wled"]}, "connectors": {"wled": table}}


def test_registration_and_doctor_are_offline_and_do_not_resolve_secrets(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_secret(_ctx: AppContext, _reference: str) -> str:
        raise AssertionError("registration must not resolve secrets")

    monkeypatch.setattr(AppContext, "secret", forbidden_secret)
    app = make_app(config(settings()))

    assert wled.NAME == "wled"
    assert wled.doctor(app.ctx) == []
    assert fake_http.requests == []
    assert getattr(wled, "MIGRATIONS", []) == []
    assert not any(job.name.startswith("wled.") for job in app.ctx.scheduler.jobs())
    assert not any(name.startswith("wled") for name in app.ctx.services)
    tools = {tool.name: tool for tool in app.registry.all() if tool.connector == "wled"}
    assert set(tools) == {
        "wled_devices",
        "wled_status",
        "wled_set_power",
        "wled_set_brightness",
        "wled_set_color",
        "wled_set_transition",
        "wled_apply_preset",
    }
    for name, tool in tools.items():
        assert tool.tier is (Tier.READ if name in {"wled_devices", "wled_status"} else Tier.ACT)
        assert tool.input_schema["additionalProperties"] is False
        assert tool.tier is Tier.READ or tool.summarize is not None


def test_disabled_connector_has_no_tools_or_calls(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app()

    assert not any(tool.connector == "wled" for tool in app.registry.all())
    assert fake_http.requests == []


def test_defaults_and_endpoint_normalization() -> None:
    table = settings()
    table["devices"][0]["base_url"] = "https://Desk.Local:443/"
    del table["devices"][0]["allow_http"]

    parsed = load_settings(table)

    assert parsed.timeout_seconds == 5
    assert parsed.max_response_bytes == 65536
    [device] = parsed.devices
    assert device.base_url == "https://desk.local:443"
    assert device.segments == (0, 1)
    assert device.allow_http is False
    assert device.allow_global is False
    assert device.presets == ()


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.50.20",
        "http://127.0.0.1",
        "http://localhost",
        "http://[::1]",
    ],
)
def test_plain_http_requires_consent_even_for_loopback(origin: str) -> None:
    table = settings()
    table["devices"][0].update(base_url=origin, allow_http=False)

    with pytest.raises(ConfigError, match="allow_http"):
        load_settings(table)


@pytest.mark.parametrize(
    "origin",
    [
        "https://192.168.50.20",
        "https://desk.local",
        "https://desk.home.arpa:8443",
        "https://[fd00::20]",
        "https://[::1]",
        "https://169.254.1.20",
    ],
)
def test_local_origins_are_accepted_without_resolution(origin: str) -> None:
    table = settings()
    table["devices"][0]["base_url"] = origin

    assert load_settings(table).devices[0].base_url == origin


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://192.168.50.20",
        "//192.168.50.20",
        "https://public.example.test",
        "https://8.8.8.8",
        "https://0.0.0.0",
        "https://224.0.0.1",
        "https://[2001:4860:4860::8888]",
        "https://desk.local/json",
        "https://desk.local//",
        "https://user:password@desk.local",
        "https://desk.local?query=value",
        "https://desk.local?",
        "https://desk.local#fragment",
        "https://desk.local#",
        "https://desk.local:0",
        "https://desk.local:65536",
        "https://desk.local:bad",
        "https://desk.local:",
        "https://desk.local\\evil",
        "https://desk.local/%2e",
        "https://[fe80::1%25eth0]",
        "https://desk.local\n",
        "https://desk.local\x00",
        "https://de\u00e9sk.local",
    ],
)
def test_unsafe_or_nonlocal_origins_are_refused(origin: str) -> None:
    table = settings()
    table["devices"][0]["base_url"] = origin

    with pytest.raises(ConfigError, match="base_url"):
        load_settings(table)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unexpected", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", 31),
        ("timeout_seconds", True),
        ("timeout_seconds", 1.5),
        ("max_response_bytes", 4095),
        ("max_response_bytes", 262145),
        ("max_response_bytes", False),
        ("devices", []),
        ("devices", "desk"),
    ],
)
def test_invalid_top_level_settings_are_refused(key: str, value: Any) -> None:
    with pytest.raises(ConfigError):
        load_settings(settings(**{key: value}))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token", "env:UNUSED_WLED_TOKEN"),
        ("allow_http", "true"),
        ("allow_global", 1),
        ("name", "Desk"),
        ("name", "x" * 33),
        ("segments", []),
        ("segments", [True]),
        ("segments", [-1]),
        ("segments", [64]),
        ("segments", [0, 0]),
        ("segments", list(range(17))),
        ("presets", [{"name": "evening", "id": 1}]),
        ("presets", [{"name": "evening", "id": True, "sha256": "a" * 64}]),
        ("presets", [{"name": "evening", "id": 251, "sha256": "a" * 64}]),
        ("presets", [{"name": "evening", "id": 1, "sha256": "A" * 64}]),
        ("presets", [{"name": "evening", "id": 1, "sha256": "a" * 64, "command": {}}]),
    ],
)
def test_invalid_device_settings_are_refused(key: str, value: Any) -> None:
    table = settings()
    table["devices"][0][key] = value

    with pytest.raises(ConfigError):
        load_settings(table)


@pytest.mark.parametrize("duplicate", ["name", "endpoint", "preset_name", "preset_id"])
def test_duplicate_configuration_is_refused(duplicate: str) -> None:
    table = settings()
    first = table["devices"][0]
    if duplicate in {"name", "endpoint"}:
        second = dict(first)
        if duplicate == "name":
            second["base_url"] = "http://192.168.50.21"
        else:
            second["name"] = "other"
        table["devices"].append(second)
    else:
        first["presets"] = [
            {"name": "evening", "id": 1, "sha256": "a" * 64},
            {
                "name": "evening" if duplicate == "preset_name" else "other",
                "id": 1 if duplicate == "preset_id" else 2,
                "sha256": "b" * 64,
            },
        ]

    with pytest.raises(ConfigError, match="unique"):
        load_settings(table)


def test_too_many_devices_are_refused() -> None:
    first = settings()["devices"][0]
    table = settings(
        devices=[
            {**first, "name": f"device_{index}", "base_url": f"http://192.168.50.{index + 1}"}
            for index in range(17)
        ]
    )

    with pytest.raises(ConfigError):
        load_settings(table)


def test_doctor_reports_bad_configuration_without_io(make_app: AppFactory, fake_http: FakeHttp) -> None:
    app = make_app()
    app.ctx.config.data["connectors"] = {"wled": {"devices": []}}

    assert wled.doctor(app.ctx)
    assert fake_http.requests == []
