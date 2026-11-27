"""Synthetic tool tests for the documents connector; no personal folders or accounts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.documents import doctor, filesystem, register
from switchboard.connectors.documents.service import Documents
from switchboard.errors import ToolError
from switchboard.policy import Origin, Tier
from switchboard.tools import ToolRegistry
from tests.conftest import AppFactory


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _write(root: Path, name: str, raw: bytes) -> Path:
    path = root / name
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _app(make_app: AppFactory, root: Path, **limits: Any) -> App:
    app = make_app(
        {"connectors": {"documents": {"collections": [{"name": "notes", "path": str(root)}], **limits}}}
    )
    register(app.ctx, app.registry)
    return app


def test_registration_is_offline_and_does_not_inspect_missing_folders(
    make_app: AppFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(
        {"connectors": {"documents": {"collections": [{"name": "notes", "path": str(tmp_path / "missing")}]}}}
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("registration attempted collection access")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    registry = ToolRegistry()
    register(app.ctx, registry)
    assert {tool.name: tool.tier for tool in registry.all()} == {
        "documents_collections": Tier.READ,
        "documents_list": Tier.READ,
        "documents_search": Tier.READ,
        "documents_read": Tier.READ,
    }
    assert all(
        tool.connector == "documents" and tool.annotations()["readOnlyHint"] for tool in registry.all()
    )
    assert app.ctx.services == {}
    assert not any(job.name.startswith("documents.") for job in app.ctx.scheduler.jobs())


def test_collection_discovery_reports_configuration_not_live_availability(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(make_app, private_root)
    private_root.rmdir()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("discovery attempted collection access")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    result = Documents(app.ctx).collections({})
    assert result["collections"] == [{"name": "notes", "extensions": [".markdown", ".md", ".txt"]}]
    assert result["availability_checked"] is False
    assert result["cached"] is False
    assert str(private_root) not in str(result)


def test_listing_is_sorted_paged_and_does_not_validate_text(make_app: AppFactory, private_root: Path) -> None:
    for name in ("z.txt", "a.md", "middle.markdown"):
        _write(private_root, name, b"\xff")
    service = Documents(_app(make_app, private_root).ctx)
    first = service.listing({"collection": "notes", "limit": 2})
    assert [item["filename"] for item in first["notes"]] == ["a.md", "middle.markdown"]
    assert first["next_offset"] == 2
    assert first["total"] == 3
    assert first["listing_complete"] is True
    assert first["text_validated"] is False
    second = service.listing({"collection": "notes", "limit": 2, "offset": first["next_offset"]})
    assert [item["filename"] for item in second["notes"]] == ["z.txt"]
    assert second["next_offset"] is None
    assert service.listing({"collection": "notes", "offset": 3})["notes"] == []


def test_listing_skips_unsupported_and_unsafe_entries_with_explicit_counts(
    make_app: AppFactory, private_root: Path
) -> None:
    _write(private_root, "safe.md", b"safe")
    _write(private_root, ".hidden.md", b"hidden")
    _write(private_root, "report.pdf", b"not parsed")
    unsafe = _write(private_root, "shared.txt", b"not private")
    unsafe.chmod(0o644)
    (private_root / "nested").mkdir(mode=0o700)
    (private_root / "alias.md").symlink_to("safe.md")
    result = Documents(_app(make_app, private_root).ctx).listing({"collection": "notes"})
    assert [item["filename"] for item in result["notes"]] == ["safe.md"]
    assert result["skipped"] == {"unsupported_name": 3, "unsafe_file": 2}
    assert all(item["untrusted_content"] for item in result["notes"])


@pytest.mark.parametrize("query", [".*", "%", "_", "[x]", "a+b", "needle\nline", "' OR 1=1 --"])
def test_search_uses_literal_case_sensitive_substrings(
    make_app: AppFactory, private_root: Path, query: str
) -> None:
    _write(private_root, "match.md", f"prefix {query} suffix".encode())
    _write(private_root, "other.md", b"unrelated note")
    result = Documents(_app(make_app, private_root).ctx).search({"collection": "notes", "query": query})
    assert [item["filename"] for item in result["results"]] == ["match.md"]
    assert result["literal"] is result["case_sensitive"] is result["coverage_complete"] is True
    assert result["results"][0]["match_offset"] == len("prefix ")


def test_search_does_not_casefold_rank_or_match_only_filenames(
    make_app: AppFactory, private_root: Path
) -> None:
    _write(private_root, "Needle.md", b"unrelated")
    _write(private_root, "a.md", b"Needle Needle Needle")
    _write(private_root, "b.md", b"needle")
    service = Documents(_app(make_app, private_root).ctx)
    upper = service.search({"collection": "notes", "query": "Needle"})
    assert [item["filename"] for item in upper["results"]] == ["a.md"]
    assert upper["matches_in_scanned_files"] == 1
    lower = service.search({"collection": "notes", "query": "needle"})
    assert [item["filename"] for item in lower["results"]] == ["b.md"]


def test_search_reads_beyond_the_default_read_preview(make_app: AppFactory, private_root: Path) -> None:
    _write(private_root, "long.md", b"x" * 6000 + b"hidden needle")
    service = Documents(_app(make_app, private_root).ctx)
    assert "needle" not in service.read({"collection": "notes", "filename": "long.md"})["text"]
    result = service.search({"collection": "notes", "query": "needle"})
    assert result["results"][0]["match_offset"] == 6007
    assert result["coverage_complete"] is True
    assert result["bytes_read"] == 6013


def test_search_does_not_treat_an_invalid_tail_as_searchable_text(
    make_app: AppFactory, private_root: Path
) -> None:
    _write(private_root, "invalid.md", b"needle\xff")
    result = Documents(_app(make_app, private_root).ctx).search({"collection": "notes", "query": "needle"})
    assert result["results"] == []
    assert result["skipped"] == {"invalid_text": 1}
    assert result["coverage_complete"] is False
    assert result["bytes_read"] == 7


def test_oversized_notes_are_not_prefix_searched(make_app: AppFactory, private_root: Path) -> None:
    _write(private_root, "large.md", b"needle " + b"x" * 20)
    service = Documents(_app(make_app, private_root, max_file_bytes=16).ctx)
    result = service.search({"collection": "notes", "query": "needle"})
    assert result["results"] == []
    assert result["coverage_complete"] is False
    assert result["skipped"] == {"too_large": 1}
    assert result["bytes_read"] == 0


def test_search_stops_at_total_byte_cap_and_does_not_imply_absence(
    make_app: AppFactory, private_root: Path
) -> None:
    _write(private_root, "a.md", b"no keyword")
    _write(private_root, "b.md", b"needle")
    service = Documents(_app(make_app, private_root, max_search_bytes=11).ctx)
    result = service.search({"collection": "notes", "query": "needle"})
    assert result["results"] == []
    assert result["bytes_read"] == 10
    assert result["files_searched"] == 1
    assert result["budget_exhausted"] is True
    assert result["coverage_complete"] is False
    assert result["next_offset"] is None
    later = service.search({"collection": "notes", "query": "needle", "offset": 1})
    assert later["coverage_complete"] is False


def test_invalid_text_consumes_aggregate_budget(make_app: AppFactory, private_root: Path) -> None:
    _write(private_root, "a.md", b"\xff" * 10)
    _write(private_root, "b.md", b"needle")
    result = Documents(_app(make_app, private_root, max_search_bytes=11).ctx).search(
        {"collection": "notes", "query": "needle"}
    )
    assert result["bytes_read"] == 10
    assert result["skipped"] == {"invalid_text": 1}
    assert result["files_searched"] == 0
    assert result["budget_exhausted"] is True
    assert result["coverage_complete"] is False


def test_search_pages_matching_files_not_occurrences(make_app: AppFactory, private_root: Path) -> None:
    for name in ("c.md", "a.md", "b.md"):
        _write(private_root, name, b"needle needle")
    service = Documents(_app(make_app, private_root).ctx)
    first = service.search({"collection": "notes", "query": "needle", "limit": 2})
    assert [item["filename"] for item in first["results"]] == ["a.md", "b.md"]
    assert first["matches_in_scanned_files"] == 3
    assert first["next_offset"] == 2
    second = service.search({"collection": "notes", "query": "needle", "limit": 2, "offset": 2})
    assert [item["filename"] for item in second["results"]] == ["c.md"]
    assert second["next_offset"] is None
    assert service.search({"collection": "notes", "query": "needle", "offset": 3})["results"] == []


def test_search_caps_aggregate_returned_text_and_marks_omitted_snippets(
    make_app: AppFactory, private_root: Path
) -> None:
    for name in ("a.md", "b.md", "c.md"):
        _write(private_root, name, b"needle " + b"x" * 500)
    result = Documents(_app(make_app, private_root, max_return_chars=10).ctx).search(
        {"collection": "notes", "query": "needle"}
    )
    assert len(result["results"]) == 3
    assert sum(len(item["snippet"]) for item in result["results"]) == result["returned_text_chars"] == 10
    assert result["results"][0]["snippet_truncated"] is True
    assert [item["text_omitted"] for item in result["results"]] == [False, True, True]
    assert result["text_truncated"] is True


def test_each_search_snippet_has_its_own_hard_cap(make_app: AppFactory, private_root: Path) -> None:
    _write(private_root, "a.md", b"needle" + b"x" * 1000)
    result = Documents(_app(make_app, private_root).ctx).search({"collection": "notes", "query": "needle"})
    assert len(result["results"][0]["snippet"]) == 400
    assert result["results"][0]["snippet_truncated"] is True


def test_read_pages_use_decoded_characters_and_honor_configured_return_cap(
    make_app: AppFactory, private_root: Path
) -> None:
    _write(private_root, "unicode.md", ("\u00e9" * 10 + "end").encode("utf-8"))
    service = Documents(_app(make_app, private_root, max_return_chars=4).ctx)
    first = service.read({"collection": "notes", "filename": "unicode.md", "max_chars": 100})
    assert first["text"] == "\u00e9" * 4
    assert first["size_bytes"] == 23
    assert first["total_chars"] == 13
    assert first["next_offset"] == 4
    assert first["text_truncated"] is first["has_more"] is True
    final = service.read({"collection": "notes", "filename": "unicode.md", "offset": 10})
    assert final["text"] == "end"
    assert final["next_offset"] is None
    assert final["has_more"] is False
    assert final["text_truncated"] is True
    past = service.read({"collection": "notes", "filename": "unicode.md", "offset": 100})
    assert past["text"] == ""
    assert past["next_offset"] is None


def test_every_response_carries_untrusted_live_provenance(make_app: AppFactory, private_root: Path) -> None:
    _write(private_root, "a.md", b"needle")
    service = Documents(_app(make_app, private_root).ctx)
    responses = [
        service.collections({}),
        service.listing({"collection": "notes"}),
        service.search({"collection": "notes", "query": "needle"}),
        service.read({"collection": "notes", "filename": "a.md"}),
    ]
    for result in responses:
        assert result["untrusted_content"] is True
        assert result["cached"] is False
        assert result["source"] == "live_filesystem"
        assert result["scope"] == "one_collection_top_level"
        assert result["freshness"] == "call_time_only"
        assert result["observed_at"]


def test_unknown_collection_is_refused_before_any_source_open(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Documents(_app(make_app, private_root).ctx)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unknown collection attempted an open")

    monkeypatch.setattr(filesystem, "_open_root", forbidden)
    with pytest.raises(ToolError, match="allowlist"):
        service.read({"collection": "not-allowed", "filename": "a.md"})


def test_removed_collection_is_not_disclosed_from_an_old_service_instance(
    make_app: AppFactory, private_root: Path, tmp_path: Path
) -> None:
    _write(private_root, "a.md", b"private synthetic content")
    app = _app(make_app, private_root)
    service = Documents(app.ctx)
    assert service.read({"collection": "notes", "filename": "a.md"})["text"]
    app.ctx.config.data["connectors"]["documents"]["collections"] = [
        {"name": "replacement", "path": str(tmp_path / "other")}
    ]
    with pytest.raises(ToolError, match="allowlist"):
        service.read({"collection": "notes", "filename": "a.md"})


def test_deleted_and_permission_revoked_files_are_not_cached(
    make_app: AppFactory, private_root: Path
) -> None:
    path = _write(private_root, "a.md", b"private synthetic content")
    service = Documents(_app(make_app, private_root).ctx)
    assert service.read({"collection": "notes", "filename": "a.md"})["text"]
    path.chmod(0o644)
    with pytest.raises(ToolError, match="unsafe_file"):
        service.read({"collection": "notes", "filename": "a.md"})
    assert service.search({"collection": "notes", "query": "private"})["results"] == []
    path.unlink()
    with pytest.raises(ToolError, match="unavailable"):
        service.read({"collection": "notes", "filename": "a.md"})
    assert service.listing({"collection": "notes"})["notes"] == []


def test_edits_between_requests_are_live_not_archived(make_app: AppFactory, private_root: Path) -> None:
    path = _write(private_root, "a.md", b"old synthetic text")
    service = Documents(_app(make_app, private_root).ctx)
    assert service.read({"collection": "notes", "filename": "a.md"})["text"] == "old synthetic text"
    path.write_bytes(b"edited synthetic text")
    assert service.read({"collection": "notes", "filename": "a.md"})["text"] == "edited synthetic text"
    assert service.search({"collection": "notes", "query": "old synthetic"})["results"] == []


def test_allowlist_revocation_during_read_discards_accumulated_content(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(private_root, "a.md", b"private synthetic text")
    app = _app(make_app, private_root)
    decode = filesystem.decode_text

    def revoke(raw: bytes) -> str:
        text = decode(raw)
        app.ctx.config.data["connectors"]["documents"]["collections"] = []
        return text

    monkeypatch.setattr(filesystem, "decode_text", revoke)
    with pytest.raises(ToolError, match="allowlist"):
        Documents(app.ctx).read({"collection": "notes", "filename": "a.md"})


def test_permission_revocation_after_decode_discards_content(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(private_root, "a.md", b"private synthetic text")
    service = Documents(_app(make_app, private_root).ctx)
    decode = filesystem.decode_text

    def revoke(raw: bytes) -> str:
        text = decode(raw)
        path.chmod(0o644)
        return text

    monkeypatch.setattr(filesystem, "decode_text", revoke)
    with pytest.raises(ToolError, match="unsafe_file"):
        service.read({"collection": "notes", "filename": "a.md"})


def test_listing_rechecks_filename_metadata_before_disclosure(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(private_root, "a.md", b"private synthetic text")
    service = Documents(_app(make_app, private_root).ctx)
    original = filesystem.LiveCollection.candidates

    def revoke(source: filesystem.LiveCollection) -> Any:
        candidates = original(source)
        path.chmod(0o644)
        return candidates

    monkeypatch.setattr(filesystem.LiveCollection, "candidates", revoke)
    with pytest.raises(ToolError, match="unsafe_file"):
        service.listing({"collection": "notes"})


def test_search_root_revocation_discards_prepared_snippets(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(private_root, "a.md", b"private synthetic needle")
    service = Documents(_app(make_app, private_root).ctx)
    decode = filesystem.decode_text

    def revoke(raw: bytes) -> str:
        text = decode(raw)
        private_root.chmod(0o755)
        return text

    monkeypatch.setattr(filesystem, "decode_text", revoke)
    with pytest.raises(ToolError, match="collection unavailable"):
        service.search({"collection": "notes", "query": "needle"})


def test_doctor_checks_only_roots_not_note_contents(
    make_app: AppFactory, private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(private_root, "a.md", b"\xff")
    app = _app(make_app, private_root)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("doctor inspected note content or enumerated a folder")

    monkeypatch.setattr(filesystem.os, "scandir", forbidden)
    monkeypatch.setattr(filesystem.os, "fdopen", forbidden)
    monkeypatch.setattr(filesystem, "decode_text", forbidden)
    assert doctor(app.ctx) == []
    private_root.chmod(0o755)
    assert "collection unavailable" in doctor(app.ctx)[0]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("documents_collections", {"path": "/srv/notes"}),
        ("documents_list", {}),
        ("documents_list", {"collection": "notes", "recursive": True}),
        ("documents_list", {"collection": "notes", "limit": 0}),
        ("documents_list", {"collection": "notes", "limit": 51}),
        ("documents_list", {"collection": "notes", "offset": -1}),
        ("documents_list", {"collection": "notes", "offset": 1001}),
        ("documents_list", {"collection": "../notes"}),
        ("documents_list", {"collection": "notes\n"}),
        ("documents_list", {"collection": "x" * 33}),
        ("documents_search", {"collection": "notes"}),
        ("documents_search", {"collection": "notes", "query": ""}),
        ("documents_search", {"collection": "notes", "query": "x" * 257}),
        ("documents_search", {"collection": "notes", "query": "x", "regex": True}),
        ("documents_search", {"collection": "notes", "query": "x", "offset": True}),
        ("documents_read", {"collection": "notes"}),
        ("documents_read", {"collection": "notes", "filename": ""}),
        ("documents_read", {"collection": "notes", "filename": "x" * 256}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "offset": -1}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "offset": 1_048_577}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "max_chars": 0}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "max_chars": 20_001}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "max_chars": 1.5}),
        ("documents_read", {"collection": "notes", "filename": "a.md", "url": "https://example.invalid"}),
    ],
)
def test_tool_schemas_bound_every_assistant_controlled_argument(
    make_app: AppFactory, private_root: Path, tool: str, args: dict[str, Any]
) -> None:
    app = _app(make_app, private_root)
    assert app.call(tool, args, Origin.ASSISTANT).status == "invalid"
