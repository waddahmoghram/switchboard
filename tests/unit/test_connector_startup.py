"""Every registered connector starts offline from a minimal configuration and refuses invalid ones.

``register`` runs on every command, so the connector guide requires it to be fast, offline, and free of
secret resolution, and the tools, tables, jobs, and services it adds to follow shared conventions. These
tests enable each connector the way the command line does, with sockets disabled, so a wiring mistake fails
here instead of on a real machine.
"""

from __future__ import annotations

import copy
import json
import re
import socket
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest

from switchboard import secrets
from switchboard.app import KNOWN_CONNECTORS, App, build_app
from switchboard.config import config_from_dict
from switchboard.errors import ConfigError
from switchboard.jsonschema import check_schema
from switchboard.mcp_server import describe_tool
from switchboard.policy import Tier
from tests.fakes import FakeHttp

SECRET_ENV = "SWITCHBOARD_TEST_STARTUP_SECRET"
SECRET = f"env:{SECRET_ENV}"
CANARY = "canary-value-that-must-never-be-printed"
CONNECTORS = sorted(KNOWN_CONNECTORS)
TOLERANT = {"mail", "finance", "shopping"}


def minimal_settings(private: Path) -> dict[str, dict[str, Any]]:
    """The smallest table each connector accepts; files and folders live under ``private``."""
    return {
        "mail": {
            "accounts": {
                "personal": {"provider": "fastmail", "address": "alex@example.com", "password": SECRET}
            }
        },
        "calendar": {"sources": {"home": {"type": "ics_file", "path": str(private / "home.ics")}}},
        "contacts": {"sources": {"phone": {"type": "vcf_file", "path": str(private / "phone.vcf")}}},
        "finance": {},
        "shopping": {},
        "notify": {},
        "webhooks": {},
        "home": {"base_url": "https://home.example.test", "token": SECRET},
        "telegram": {"bot_token": SECRET, "allowed_user_ids": [111], "allowed_chat_ids": [111]},
        "linkedin": {},
        "whatsapp": {"date_order": "DMY"},
        "wled": {
            "devices": [
                {"name": "desk", "base_url": "http://192.168.50.20", "allow_http": True, "segments": [0]}
            ]
        },
        "shelly": {
            "devices": {
                "lamp": {
                    "base_url": "http://192.168.10.40",
                    "allow_http": True,
                    "expected_device_id": "shellyplusplugs-000000000001",
                    "expected_model": "SNPL-00112EU",
                    "expected_generation": 2,
                    "switch_ids": [0],
                }
            }
        },
        "documents": {"collections": [{"name": "notes", "path": str(private), "extensions": [".md"]}]},
        "tasks": {},
        "monitoring": {
            "checks": {"server": {"kind": "endpoint", "url": "https://health.example.test/ready"}}
        },
    }


def invalid_settings(private: Path) -> dict[str, tuple[dict[str, Any], str]]:
    """One right-typed but invalid table per connector and the problem it must produce."""
    minimal = minimal_settings(private)
    return {
        "mail": (
            {"accounts": {"personal": {"provider": "carrier-pigeon", "address": "alex@example.com"}}},
            "account personal: provider must be one of",
        ),
        "calendar": (
            {**minimal["calendar"], "refresh_interval_seconds": 5},
            "refresh_interval_seconds must be a whole number from 60",
        ),
        "contacts": (
            {**minimal["contacts"], "refresh_interval_seconds": 5},
            "refresh_interval_seconds must be a whole number from 300",
        ),
        "finance": ({"alert_rules": {"name": "not an array"}}, "alert_rules must be an array of tables"),
        "shopping": (
            {"allowed_domains": ["not a host"]},
            "allowed_domains entry 'not a host' is not a host name",
        ),
        "notify": (
            {"channels": {"phone": {"type": "carrier-pigeon"}}},
            "type must be ntfy, telegram, or email",
        ),
        "webhooks": ({"hooks": {"ci": {"secret": SECRET, "verify": "basic"}}}, "verify must be one of"),
        "home": ({**minimal["home"], "base_url": "ftp://home.example.test"}, "base_url must be an https://"),
        "telegram": (
            {**minimal["telegram"], "bot_token": "123456:plain-text-token"},
            "bot_token must be a secret reference",
        ),
        "linkedin": ({"publishing_enabled": True}, "publishing requires your author_urn"),
        "whatsapp": ({"date_order": "YDM"}, "date_order is required and must be MDY, DMY, or YMD"),
        "wled": ({"devices": []}, "needs at least 1 items"),
        "shelly": ({"devices": {}}, "devices must contain 1 to 16 named device tables"),
        "documents": ({"collections": []}, "configure between 1 and 16 explicit documents collections"),
        "tasks": ({"unknown_setting": 1}, "unknown settings: unknown_setting"),
        "monitoring": ({"checks": {}}, "checks must contain 1-32 named tables"),
    }


@dataclass
class Probe:
    """What start-up touched that the connector guide says it must not."""

    network: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Baseline:
    """What the core adds on its own, so connector additions can be told apart."""

    tools: frozenset[str]
    tables: frozenset[str]
    jobs: frozenset[str]
    services: frozenset[str]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    probe = Probe()

    def refuse(name: str) -> Callable[..., NoReturn]:
        def blocked(*_args: object, **_kwargs: object) -> NoReturn:
            probe.network.append(name)
            raise OSError(f"networking is disabled in this test ({name})")

        return blocked

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    real_resolve = secrets.resolve

    def recording_resolve(reference: str, *, timeout: float = 20.0) -> str:
        probe.secrets.append(reference)
        return real_resolve(reference, timeout=timeout)

    monkeypatch.setattr(secrets, "resolve", recording_resolve)
    monkeypatch.setenv(SECRET_ENV, CANARY)
    return probe


@pytest.fixture
def private(tmp_path: Path) -> Path:
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    return folder


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Baseline:
    return snapshot(start([], {}, tmp_path_factory.mktemp("core"), FakeHttp()))


def start(names: list[str], tables: Mapping[str, Any], data_dir: Path, http: FakeHttp) -> App:
    data = {"switchboard": {"connectors": names}, "connectors": dict(tables)}
    return build_app(config_from_dict(data, data_dir), http=http)


def table_names(app: App) -> set[str]:
    with app.ctx.db.session() as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def snapshot(app: App) -> Baseline:
    return Baseline(
        tools=frozenset(tool.name for tool in app.registry.all()),
        tables=frozenset(table_names(app)),
        jobs=frozenset(job.name for job in app.ctx.scheduler.jobs()),
        services=frozenset(app.ctx.services),
    )


def fixed_width(pattern: object) -> bool:
    """An anchored pattern without open-ended repetition also limits a string's length."""
    return (
        isinstance(pattern, str)
        and pattern.startswith("^")
        and pattern.endswith("$")
        and re.search(r"[*+]|\{\d+,\}", pattern) is None
    )


def unbounded(schema: Mapping[str, Any], where: str) -> list[str]:
    """Places where ``schema`` accepts values of unlimited size, which the connector guide forbids."""
    found: list[str] = []
    declared = schema.get("type")
    kinds = set(declared) if isinstance(declared, list) else {declared}
    fixed = "enum" in schema or "const" in schema
    if (
        "string" in kinds
        and not fixed
        and "maxLength" not in schema
        and not fixed_width(schema.get("pattern"))
    ):
        found.append(f"{where}: string without maxLength")
    if "array" in kinds and "maxItems" not in schema:
        found.append(f"{where}: array without maxItems")
    if kinds & {"integer", "number"} and not fixed and not {"minimum", "maximum"}.issubset(schema):
        found.append(f"{where}: number without minimum and maximum")
    if (
        "object" in kinds
        and schema.get("additionalProperties") is not False
        and "maxProperties" not in schema
    ):
        found.append(f"{where}: open object without maxProperties")
    for key, child in schema.get("properties", {}).items():
        found += unbounded(child, f"{where}.{key}")
    for keyword in ("items", "additionalProperties", "propertyNames"):
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            found += unbounded(child, f"{where}.{keyword}")
    return found


def settings_for(name: str, private: Path) -> dict[str, Any]:
    tables = minimal_settings(private)
    if name not in tables:
        pytest.fail(f"add a minimal configuration for the new connector {name!r} to this test")
    return tables[name]


def paths(value: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Every key and index inside a settings table, outermost first."""
    children: Iterable[tuple[Any, Any]]
    if isinstance(value, dict):
        children = value.items()
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return
    for key, child in children:
        yield (*prefix, key)
        yield from paths(child, (*prefix, key))


def replaced(table: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    clone = copy.deepcopy(table)
    target: Any = clone
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return clone


def wrong_type(value: Any) -> Any:
    """A value of a different TOML type that still looks plausible to a hurried editor."""
    if isinstance(value, bool):
        return "yes"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return 1
    if isinstance(value, list):
        return {"first": value[0] if value else "x"}
    return ["x"]


@pytest.mark.parametrize("name", CONNECTORS)
def test_connector_starts_offline_and_registers_conforming_tools(
    name: str, private: Path, tmp_path: Path, probe: Probe, baseline: Baseline
) -> None:
    http = FakeHttp()
    app = start([name], {name: settings_for(name, private)}, tmp_path / "data", http)

    assert probe.network == []
    assert http.requests == []
    assert probe.secrets == [], "register must resolve secrets lazily, inside handlers and jobs"
    tools = [tool for tool in app.registry.all() if tool.name not in baseline.tools]
    assert tools, f"{name} registered no tools"
    for tool in tools:
        assert tool.connector == name, f"{tool.name} would run outside the {name} lane"
        assert tool.name.startswith(f"{name}_")
        assert tool.description.strip()
        check_schema(tool.input_schema)
        assert tool.input_schema.get("additionalProperties") is False, tool.name
        assert unbounded(tool.input_schema, tool.name) == []
        if tool.tier in {Tier.ACT, Tier.SPEND}:
            assert tool.summarize is not None, f"{tool.name} needs a one-line approval summary"
        listed = json.loads(json.dumps(describe_tool(tool)))
        assert listed["name"] == tool.name
        assert listed["inputSchema"] == json.loads(json.dumps(tool.input_schema))
    added = snapshot(app)
    assert all(table.startswith(f"{name}_") for table in added.tables - baseline.tables)
    for job in app.ctx.scheduler.jobs():
        if job.name not in baseline.jobs:
            assert job.name.startswith(f"{name}.")
            assert job.lane == name, f"{job.name} would not serialize with the {name} tools"
    for service in added.services - baseline.services:
        assert service == name or service.startswith(f"{name}."), service


@pytest.mark.parametrize("name", CONNECTORS)
def test_doctor_reports_problems_offline_without_echoing_secrets(
    name: str, private: Path, tmp_path: Path, probe: Probe
) -> None:
    http = FakeHttp()
    app = start([name], {name: settings_for(name, private)}, tmp_path / "data", http)
    doctor = getattr(app.connectors[name], "doctor", None)
    if doctor is None:
        pytest.skip(f"{name} has no doctor check")

    problems = doctor(app.ctx)

    assert isinstance(problems, list)
    assert all(isinstance(problem, str) and problem for problem in problems)
    assert not [problem for problem in problems if CANARY in problem]
    assert probe.network == []
    assert http.requests == []


def test_every_connector_starts_together_in_either_order(private: Path, tmp_path: Path, probe: Probe) -> None:
    tables = minimal_settings(private)
    forward = start(CONNECTORS, tables, tmp_path / "forward", FakeHttp())
    backward = start(CONNECTORS[::-1], tables, tmp_path / "backward", FakeHttp())

    assert {tool.connector for tool in forward.registry.all()} == {"core", *KNOWN_CONNECTORS}
    assert snapshot(forward) == snapshot(backward)
    assert probe.network == []
    assert probe.secrets == []


@pytest.mark.parametrize("name", CONNECTORS)
def test_section_that_is_not_a_table_is_a_configuration_error(
    name: str, tmp_path: Path, probe: Probe
) -> None:
    with pytest.raises(ConfigError, match=rf"\[connectors\.{name}\] must be a table"):
        start([name], {name: ["not", "a", "table"]}, tmp_path / "data", FakeHttp())

    assert probe.network == []


@pytest.mark.parametrize("name", CONNECTORS)
def test_invalid_setting_is_refused_or_reported_by_doctor(
    name: str, private: Path, tmp_path: Path, probe: Probe
) -> None:
    table, problem = invalid_settings(private)[name]
    if name not in TOLERANT:
        with pytest.raises(ConfigError, match=re.escape(problem)):
            start([name], {name: table}, tmp_path / "data", FakeHttp())
        return

    # These connectors keep their other settings working and explain the broken one in doctor.
    app = start([name], {name: table}, tmp_path / "data", FakeHttp())
    assert any(problem in line for line in app.connectors[name].doctor(app.ctx))
    assert probe.network == []


@pytest.mark.parametrize("name", CONNECTORS)
def test_wrongly_typed_setting_is_a_configuration_error_not_a_crash(
    name: str, private: Path, tmp_path: Path, probe: Probe
) -> None:
    table = settings_for(name, private)
    for index, path in enumerate(paths(table)):
        target: Any = table
        for key in path:
            target = target[key]
        variant = replaced(table, path, wrong_type(target))
        try:
            app = start([name], {name: variant}, tmp_path / f"variant-{index}", FakeHttp())
        except ConfigError:
            continue
        problems = app.connectors[name].doctor(app.ctx)
        assert all(isinstance(problem, str) for problem in problems), path
    assert probe.network == []
