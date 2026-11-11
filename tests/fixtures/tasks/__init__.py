"""Synthetic local task setup; no external account or service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from switchboard.app import App
from switchboard.connectors.tasks.service import TaskService
from tests.conftest import AppFactory

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
PAST = "2026-11-12T11:00:00+00:00"
FUTURE = "2026-11-12T13:00:00+00:00"


def configured(make_app: AppFactory, **settings: Any) -> tuple[App, TaskService]:
    app = make_app({"switchboard": {"connectors": ["tasks"]}, "connectors": {"tasks": settings}})
    service = app.ctx.require("tasks.local")
    assert isinstance(service, TaskService)
    service.clock = lambda: NOW
    return app, service
