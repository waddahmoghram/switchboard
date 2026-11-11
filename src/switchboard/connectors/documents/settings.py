"""Explicit, shallow note collections and non-negotiable resource ceilings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from switchboard.errors import ConfigError

NAME = "documents"
EXTENSIONS = (".md", ".markdown", ".txt")
MAX_COLLECTIONS = 16
MAX_ENTRIES = 1000
MAX_FILE_BYTES = 1_048_576
MAX_SEARCH_BYTES = 8_388_608
MAX_RETURN_CHARS = 20_000
MAX_PAGE = 50
MAX_QUERY_CHARS = 256
MAX_FILENAME_BYTES = 255
COLLECTION_PATTERN = r"[a-z][a-z0-9_-]{0,31}"

_LIMITS = {
    "max_entries": (256, MAX_ENTRIES),
    "max_file_bytes": (262_144, MAX_FILE_BYTES),
    "max_search_bytes": (2_097_152, MAX_SEARCH_BYTES),
    "max_return_chars": (8000, MAX_RETURN_CHARS),
}


@dataclass(frozen=True)
class Collection:
    name: str
    path: Path
    extensions: tuple[str, ...] = EXTENSIONS


@dataclass(frozen=True)
class Settings:
    collections: tuple[Collection, ...]
    max_entries: int
    max_file_bytes: int
    max_search_bytes: int
    max_return_chars: int


def _collection(raw: Any) -> Collection:
    if not isinstance(raw, dict) or set(raw) - {"name", "path", "extensions"}:
        raise ConfigError("each documents collection must contain only name, path, and optional extensions")
    name = raw.get("name")
    if not isinstance(name, str) or re.fullmatch(COLLECTION_PATTERN, name) is None:
        raise ConfigError("documents collection names must be short lowercase identifiers")
    directory = raw.get("path")
    if (
        not isinstance(directory, str)
        or not directory
        or len(directory) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in directory)
    ):
        raise ConfigError("documents collection paths must be explicit absolute private folder paths")
    try:
        directory.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("documents collection paths must contain valid text") from exc
    path = Path(directory)
    if not path.is_absolute() or ".." in path.parts or directory.startswith("//"):
        raise ConfigError(
            "documents paths must be absolute, without traversal, home shorthand, or network paths"
        )
    home = Path.home()
    working = Path.cwd()
    if path in {home, working, *home.parents, *working.parents}:
        raise ConfigError("use a dedicated documents subfolder, not a home, working folder, or an ancestor")
    extensions = raw.get("extensions", list(EXTENSIONS))
    if (
        not isinstance(extensions, list)
        or not 1 <= len(extensions) <= len(EXTENSIONS)
        or not all(isinstance(item, str) and item in EXTENSIONS for item in extensions)
        or len(set(extensions)) != len(extensions)
    ):
        raise ConfigError("documents extensions must be a nonempty unique subset of .md, .markdown, and .txt")
    return Collection(name, path, tuple(sorted(extensions)))


def load_settings(raw: dict[str, Any]) -> Settings:
    """Validate configuration only; never inspect a collection or resolve a symbolic link."""
    if set(raw) - {"collections", *_LIMITS}:
        raise ConfigError(
            "[connectors.documents] has unsupported settings; recursive access is not supported"
        )
    entries = raw.get("collections")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_COLLECTIONS:
        raise ConfigError(f"configure between 1 and {MAX_COLLECTIONS} explicit documents collections")
    collections = tuple(sorted((_collection(entry) for entry in entries), key=lambda item: item.name))
    if len({item.name for item in collections}) != len(collections):
        raise ConfigError("documents collection names must be unique")
    if len({item.path for item in collections}) != len(collections):
        raise ConfigError("documents collection paths must be unique")
    limits: dict[str, int] = {}
    for key, (default, maximum) in _LIMITS.items():
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ConfigError(f"documents {key} must be a whole number between 1 and {maximum}")
        limits[key] = value
    return Settings(collections=collections, **limits)
