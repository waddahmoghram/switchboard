"""Synthetic configuration cases; no real collection is inspected."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors.documents.settings import (
    EXTENSIONS,
    MAX_COLLECTIONS,
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_RETURN_CHARS,
    MAX_SEARCH_BYTES,
    load_settings,
)
from switchboard.errors import ConfigError


def _raw(**overrides: Any) -> dict[str, Any]:
    return {"collections": [{"name": "work", "path": "/srv/switchboard-notes/work"}], **overrides}


def test_defaults_and_explicit_format_subset() -> None:
    settings = load_settings(_raw())
    assert settings.collections[0].name == "work"
    assert settings.collections[0].path == Path("/srv/switchboard-notes/work")
    assert set(settings.collections[0].extensions) == set(EXTENSIONS)
    assert (settings.max_entries, settings.max_file_bytes) == (256, 262_144)
    assert (settings.max_search_bytes, settings.max_return_chars) == (2_097_152, 8000)
    restricted = load_settings(
        _raw(collections=[{"name": "work", "path": "/srv/switchboard-notes/work", "extensions": [".md"]}])
    )
    assert restricted.collections[0].extensions == (".md",)


def test_configuration_loading_performs_no_collection_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("configuration attempted filesystem access")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    assert load_settings(_raw()).collections[0].name == "work"


@pytest.mark.parametrize("collections", [None, [], {}, "all", [None], ["/srv/notes"]])
def test_missing_or_malformed_collections_are_refused(collections: Any) -> None:
    with pytest.raises(ConfigError):
        load_settings(_raw(collections=collections))


def test_collection_count_has_a_real_hard_ceiling() -> None:
    entries = [
        {"name": f"set-{index}", "path": f"/srv/switchboard-notes/set-{index}"}
        for index in range(MAX_COLLECTIONS)
    ]
    assert len(load_settings(_raw(collections=entries)).collections) == MAX_COLLECTIONS
    with pytest.raises(ConfigError, match="between"):
        load_settings(_raw(collections=[*entries, {"name": "extra", "path": "/srv/extra-notes"}]))


@pytest.mark.parametrize("name", ["", "Work", "work notes", "../work", "x\n", "a" * 33, 1, None])
def test_collection_names_are_bounded_identifiers(name: Any) -> None:
    with pytest.raises(ConfigError, match="names"):
        load_settings(_raw(collections=[{"name": name, "path": "/srv/switchboard-notes/work"}]))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "notes",
        "~/notes",
        "/srv/../notes",
        "//server/notes",
        "https://example.invalid/notes",
        "/srv/notes\x00",
        "/srv/notes\n",
        "/srv/\udcff",
        "/" + "x" * 4096,
        None,
        1,
    ],
)
def test_paths_must_be_explicit_local_absolute_paths(path: Any) -> None:
    with pytest.raises(ConfigError, match="paths"):
        load_settings(_raw(collections=[{"name": "work", "path": path}]))


def test_home_working_directory_and_ancestors_are_not_collections() -> None:
    for path in {Path("/"), Path.home(), Path.cwd(), *Path.home().parents, *Path.cwd().parents}:
        with pytest.raises(ConfigError, match="dedicated"):
            load_settings(_raw(collections=[{"name": "work", "path": str(path)}]))


@pytest.mark.parametrize("duplicate", ["name", "path"])
def test_duplicate_names_and_paths_are_refused(duplicate: str) -> None:
    first = {"name": "one", "path": "/srv/switchboard-notes/one"}
    second = {"name": "two", "path": "/srv/switchboard-notes/two", duplicate: first[duplicate]}
    with pytest.raises(ConfigError, match="unique"):
        load_settings(_raw(collections=[first, second]))


@pytest.mark.parametrize(
    "extensions", [[], [".pdf"], [".docx"], [".html"], [".MD"], [".md", ".md"], ".md", [1], None]
)
def test_extensions_are_a_declared_plain_text_subset(extensions: Any) -> None:
    with pytest.raises(ConfigError, match="extensions"):
        load_settings(
            _raw(
                collections=[
                    {"name": "work", "path": "/srv/switchboard-notes/work", "extensions": extensions}
                ]
            )
        )


@pytest.mark.parametrize(
    ("key", "maximum"),
    [
        ("max_entries", MAX_ENTRIES),
        ("max_file_bytes", MAX_FILE_BYTES),
        ("max_search_bytes", MAX_SEARCH_BYTES),
        ("max_return_chars", MAX_RETURN_CHARS),
    ],
)
def test_numeric_limits_accept_their_boundaries(key: str, maximum: int) -> None:
    assert getattr(load_settings(_raw(**{key: 1})), key) == 1
    assert getattr(load_settings(_raw(**{key: maximum})), key) == maximum
    for invalid in (0, -1, True, 1.5, "12", maximum + 1):
        with pytest.raises(ConfigError, match=key):
            load_settings(_raw(**{key: invalid}))


@pytest.mark.parametrize("key", ["recursive", "index", "follow_links", "url", "command", "maxFiles"])
def test_unknown_top_level_settings_are_not_silently_ignored(key: str) -> None:
    with pytest.raises(ConfigError, match="unsupported"):
        load_settings(_raw(**{key: True}))


def test_unknown_collection_fields_are_not_silently_ignored() -> None:
    with pytest.raises(ConfigError, match="only name"):
        load_settings(
            _raw(collections=[{"name": "work", "path": "/srv/switchboard-notes/work", "recursive": True}])
        )


def test_collection_order_does_not_change_authorization_identity() -> None:
    entries = [
        {"name": "two", "path": "/srv/switchboard-notes/two"},
        {"name": "one", "path": "/srv/switchboard-notes/one"},
    ]
    assert load_settings(_raw(collections=entries)) == load_settings(
        _raw(collections=list(reversed(entries)))
    )
