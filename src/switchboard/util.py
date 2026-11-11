"""Small shared helpers: UTC time, hashing, canonical JSON, and glob matching."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """Render an aware datetime as a UTC ISO 8601 string that sorts lexicographically."""
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; attach a time zone first")
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(now())


def iso_after(seconds: float) -> str:
    return iso(now() + timedelta(seconds=seconds))


def iso_before(seconds: float) -> str:
    return iso(now() - timedelta(seconds=seconds))


def canonical_json(value: Any) -> str:
    """Deterministic, ASCII-only JSON used for hashing and storage (lone surrogates stay escaped)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def json_loads(value: str | bytes) -> Any:
    """Read JSON without accepting NaN, infinity, or overflowing floating-point literals."""
    return json.loads(value, parse_constant=_invalid_number, parse_float=_finite_float)


def json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating booleans as numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _invalid_number(value: str) -> NoReturn:
    raise ValueError(f"{value} is not a finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _invalid_number(value)
    return number


def printable(text: str) -> str:
    """Escape control, format, and other non-printable characters so terminal output cannot be spoofed.

    An escape sequence or a right-to-left override in an assistant-chosen subject could otherwise hide or
    reorder what an approval prompt shows.
    """
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in text)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def glob_any(value: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive shell-style match against any pattern."""
    lowered = value.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking the cut."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"
