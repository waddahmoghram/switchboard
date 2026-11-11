"""Read-only measurements and explicit evidence criteria; response bodies never leave this module."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from shutil import disk_usage
from typing import Any, NoReturn

from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError
from switchboard.util import json_equal

from .files import MAX_REPORT_BYTES, CheckError, read_report
from .settings import MAX_BYTES, BackupCheck, Check, DiskCheck, EndpointCheck

MAX_RESPONSE_BYTES = 65536
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


@dataclass(frozen=True)
class Observation:
    status: str
    error: str | None = None
    reachable: bool | None = None
    ready: bool | None = None
    measurements: dict[str, int | float] = field(default_factory=dict)
    report: dict[str, str] = field(default_factory=dict)


def measure(ctx: AppContext, check: Check, clock: Callable[[], datetime]) -> Observation:
    """Perform exactly the configured operation, containing expected and unexpected failures."""
    try:
        if isinstance(check, EndpointCheck):
            return _endpoint(ctx, check)
        if isinstance(check, BackupCheck):
            return _backup(check, clock)
        return _disk(check)
    except CheckError as exc:
        return Observation("error", str(exc))
    except Exception:
        return Observation("error", "check_failed")


def _endpoint(ctx: AppContext, check: EndpointCheck) -> Observation:
    headers = {"Accept": "application/json"}
    if check.bearer_token is not None:
        try:
            token = ctx.secret(check.bearer_token)
        except SecretError:
            return Observation("error", "secret_unavailable")
        if not 1 <= len(token) <= 4096 or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
            return Observation("error", "secret_invalid")
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = ctx.http.request(
            "GET",
            check.url,
            headers=headers,
            timeout=check.timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
            allow_http=check.allow_http,
            follow_redirects=False,
            public_only=False,
        )
    except HttpError:
        return Observation("error", "endpoint_unreachable", reachable=False)
    measurements: dict[str, int | float] = {
        "http_status": response.status,
        "response_bytes": len(response.body),
    }
    not_ready = False if check.readiness_field is not None else None
    if 300 <= response.status <= 399 or response.url != check.url:
        return Observation("unhealthy", "redirect_refused", True, not_ready, measurements)
    if response.status != check.expected_status:
        return Observation("unhealthy", "unexpected_http_status", True, not_ready, measurements)
    if len(response.body) > MAX_RESPONSE_BYTES:
        return Observation("error", "response_too_large", True, not_ready, measurements)
    if check.readiness_field is None:
        return Observation("reachable", reachable=True, measurements=measurements)
    try:
        document = _object(response.body, MAX_RESPONSE_BYTES)
    except CheckError:
        return Observation("error", "response_malformed", True, False, measurements)
    value: Any = document
    for key in check.readiness_field.split("."):
        if not isinstance(value, dict) or key not in value:
            return Observation("unhealthy", "readiness_field_missing", True, False, measurements)
        value = value[key]
    if not json_equal(value, check.readiness_equals):
        return Observation("unhealthy", "readiness_mismatch", True, False, measurements)
    return Observation("healthy", reachable=True, ready=True, measurements=measurements)


def _backup(check: BackupCheck, clock: Callable[[], datetime]) -> Observation:
    try:
        document = _object(read_report(check), MAX_REPORT_BYTES)
    except CheckError as exc:
        if str(exc) == "invalid_document":
            raise CheckError("report_malformed") from exc
        raise
    required = {"schema_version", "check", "status", "started_at", "finished_at"}
    if set(document) - (required | {"bytes_written"}) or not required <= set(document):
        raise CheckError("report_schema_invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise CheckError("report_schema_invalid")
    if document["check"] != check.name:
        raise CheckError("report_check_mismatch")
    if document["status"] not in ("succeeded", "failed"):
        raise CheckError("report_schema_invalid")
    started = _timestamp(document["started_at"])
    finished = _timestamp(document["finished_at"])
    checked_at = clock()
    if not started <= finished <= checked_at:
        raise CheckError("report_time_invalid")
    age = (checked_at - finished).total_seconds()
    measurements: dict[str, int | float] = {
        "age_seconds": age,
        "duration_seconds": (finished - started).total_seconds(),
        "max_age_seconds": check.max_age_seconds,
    }
    if "bytes_written" in document:
        size = document["bytes_written"]
        if type(size) is not int or not 0 <= size <= MAX_BYTES:
            raise CheckError("report_schema_invalid")
        measurements["bytes_written"] = size
    report = {
        "status": document["status"],
        "started_at": started.isoformat(timespec="microseconds"),
        "finished_at": finished.isoformat(timespec="microseconds"),
    }
    if age > check.max_age_seconds:
        return Observation("stale", "backup_report_stale", measurements=measurements, report=report)
    if document["status"] == "failed":
        return Observation("unhealthy", "backup_failed", measurements=measurements, report=report)
    return Observation("healthy", measurements=measurements, report=report)


def _disk(check: DiskCheck) -> Observation:
    try:
        usage = disk_usage(check.path)
    except (OSError, NotImplementedError):
        return Observation("error", "disk_unavailable")
    total, used, free = usage
    if (
        any(type(value) is not int or not 0 <= value <= MAX_BYTES for value in usage)
        or total == 0
        or used + free > total
    ):
        return Observation("error", "disk_data_invalid")
    percent = free / total * 100
    measurements: dict[str, int | float] = {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "free_percent": percent,
    }
    if check.min_free_bytes is not None:
        measurements["min_free_bytes"] = check.min_free_bytes
    if check.min_free_percent is not None:
        measurements["min_free_percent"] = check.min_free_percent
    low = (check.min_free_bytes is not None and free < check.min_free_bytes) or (
        check.min_free_percent is not None and percent < check.min_free_percent
    )
    return Observation(
        "unhealthy" if low else "healthy", "disk_below_threshold" if low else None, measurements=measurements
    )


def _object(raw: bytes, limit: int) -> dict[str, Any]:
    if len(raw) > limit:
        raise CheckError("invalid_document")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid, parse_float=_finite
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckError("invalid_document") from exc
    if not isinstance(value, dict):
        raise CheckError("invalid_document")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate property")
        result[key] = value
    return result


def _invalid(_value: str) -> NoReturn:
    raise ValueError("non-finite number")


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _invalid(value)
    return number


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value) or value.endswith("-00:00"):
        raise CheckError("report_time_invalid")
    try:
        stamp = datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise CheckError("report_time_invalid") from exc
    if stamp.year < 1970:
        raise CheckError("report_time_invalid")
    return stamp
