"""Pytest fixtures shared by unit and integration tests."""

from __future__ import annotations

import os
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
def open_descriptors() -> Callable[[], int]:
    """Count this process's open file descriptors; skip where the platform cannot list them."""
    listing = next((path for path in ("/proc/self/fd", "/dev/fd") if os.path.isdir(path)), None)
    if listing is None:
        pytest.skip("this platform cannot list open file descriptors")
    return lambda: len(os.listdir(listing))


@pytest.fixture
def make_app(data_dir: Path, fake_http: FakeHttp) -> AppFactory:
    """Build a full app with a private temporary data directory and no real network.

    ``make_app({"switchboard": {"connectors": ["mail"]}, "connectors": {"mail": {...}}})``; pass
    ``http=HttpClient()`` for integration tests that talk to a local server on 127.0.0.1.
    """

    def factory(data: dict[str, Any] | None = None, *, http: HttpTransport | None = None) -> App:
        return build_app(config_from_dict(data or {}, data_dir), http=http if http is not None else fake_http)

    return factory
