"""Pytest fixtures shared by unit and integration tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App, build_app
from switchboard.config import config_from_dict
from switchboard.httpclient import HttpTransport
from tests.fakes import FakeHttp

AppFactory = Callable[..., App]


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def make_app(data_dir: Path, fake_http: FakeHttp) -> AppFactory:
    """Build a full app with a private temporary data directory and no real network.

    ``make_app({"switchboard": {"connectors": ["mail"]}, "connectors": {"mail": {...}}})``; pass
    ``http=HttpClient()`` for integration tests that talk to a local server on 127.0.0.1.
    """

    def factory(data: dict[str, Any] | None = None, *, http: HttpTransport | None = None) -> App:
        return build_app(config_from_dict(data or {}, data_dir), http=http if http is not None else fake_http)

    return factory
