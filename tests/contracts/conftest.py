"""Registers the ``contracts`` marker for the shared cross-engine contract runner."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "contracts: shared cross-engine contract fixtures from contracts/cases; no network access"
    )
