"""Offline configuration requires explicit date interpretation and has no live-access settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors.whatsapp.settings import load_settings
from switchboard.errors import ConfigError


@pytest.mark.parametrize("raw", [{}, {"date_order": None}, {"date_order": "auto"}, {"date_order": "mdy"}])
def test_date_order_is_mandatory(raw: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="date_order is required"):
        load_settings(raw)


@pytest.mark.parametrize("date_order", ["MDY", "DMY", "YMD"])
def test_explicit_orders_and_optional_timezone_are_preserved(tmp_path: Path, date_order: str) -> None:
    settings = load_settings(
        {"import_dir": str(tmp_path), "date_order": date_order, "timezone": "America/Los_Angeles"}
    )
    assert settings.import_dir == tmp_path
    assert settings.date_order == date_order
    assert settings.timezone == "America/Los_Angeles"


def test_import_folder_can_be_absent_for_historical_reads() -> None:
    settings = load_settings({"date_order": "MDY"})
    assert settings.import_dir is None
    assert settings.timezone is None


@pytest.mark.parametrize("key", ["token", "password", "session_cookie", "live", "poll_seconds", "enabled"])
def test_unknown_and_live_settings_are_refused(key: str) -> None:
    with pytest.raises(ConfigError, match="unsupported settings"):
        load_settings({"date_order": "MDY", key: "unsupported"})


@pytest.mark.parametrize("directory", ["", "relative/exports", "\x00", True, 123])
def test_import_directory_must_be_an_absolute_path_string(directory: Any) -> None:
    with pytest.raises(ConfigError, match="absolute private folder"):
        load_settings({"date_order": "MDY", "import_dir": directory})


def test_unresolvable_home_prefix_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_path: Path) -> Path:
        raise RuntimeError("synthetic unavailable home")

    monkeypatch.setattr(Path, "expanduser", fail)
    with pytest.raises(ConfigError, match="cannot resolve"):
        load_settings({"date_order": "MDY", "import_dir": "~unavailable/exports"})


@pytest.mark.parametrize("timezone", ["", " ", "x" * 101, "Zone\nother", 123, True])
def test_timezone_is_bounded_plain_context(timezone: Any) -> None:
    with pytest.raises(ConfigError, match="timezone"):
        load_settings({"date_order": "MDY", "timezone": timezone})
