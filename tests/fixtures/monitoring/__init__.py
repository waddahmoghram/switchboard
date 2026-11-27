"""Synthetic monitoring inputs for tests; no host inspection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, NamedTuple

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
ENDPOINT = "https://health.example.test/ready"
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_RESPONSE_DO_NOT_STORE"
TOKEN = "synthetic-monitoring-token"


class Usage(NamedTuple):
    total: int
    used: int
    free: int


def endpoint(**updates: Any) -> dict[str, Any]:
    return {
        "kind": "endpoint",
        "url": ENDPOINT,
        "readiness_field": "health.ready",
        "readiness_equals": True,
        **updates,
    }


def backup(**updates: Any) -> dict[str, Any]:
    return {
        "kind": "backup",
        "directory": "/synthetic-private/backup-status",
        "filename": "backup.json",
        "max_age_seconds": 3600,
        **updates,
    }


def disk(**updates: Any) -> dict[str, Any]:
    return {
        "kind": "disk",
        "path": "/synthetic-disk",
        "min_free_bytes": 200,
        "min_free_percent": 20,
        **updates,
    }


def config(checks: dict[str, Any] | None = None, **settings: Any) -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["monitoring"]},
        "connectors": {
            "monitoring": {
                "checks": {"server": endpoint(), "backup": backup(), "disk": disk()}
                if checks is None
                else checks,
                **settings,
            }
        },
    }


def report(**updates: Any) -> bytes:
    value = {
        "schema_version": 1,
        "check": "backup",
        "status": "succeeded",
        "started_at": "2030-01-02T10:00:00+00:00",
        "finished_at": "2030-01-02T11:00:00+00:00",
        "bytes_written": 4096,
        **updates,
    }
    return json.dumps(value).encode("utf-8")
