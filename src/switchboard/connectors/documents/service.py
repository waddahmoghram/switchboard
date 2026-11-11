"""Bounded live collection discovery, listing, literal search, and paged reading."""

from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager
from typing import Any

from switchboard.connectors.documents.filesystem import (
    LiveCollection,
    NoteUnavailable,
    ReadBudget,
    SearchBudgetExhausted,
    open_collection,
)
from switchboard.connectors.documents.settings import NAME, Collection, Settings, load_settings
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.util import now_iso

SNIPPET_CHARS = 400
_PROVENANCE = {
    "untrusted_content": True,
    "source": "live_filesystem",
    "cached": False,
    "scope": "one_collection_top_level",
    "freshness": "call_time_only",
}


def _provenance() -> dict[str, Any]:
    return {**_PROVENANCE, "observed_at": now_iso()}


class Documents:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _settings(self) -> Settings:
        try:
            return load_settings(self.ctx.connector_config(NAME))
        except ConfigError as exc:
            raise ToolError("documents allowlist is missing or invalid; reload valid configuration") from exc

    def _recheck(self, settings: Settings) -> None:
        if self._settings() != settings:
            raise ToolError("documents allowlist or limits changed during the request; retry")

    def _source(self, settings: Settings, name: str) -> AbstractContextManager[LiveCollection]:
        collection = next((item for item in settings.collections if item.name == name), None)
        if collection is None:
            raise ToolError("collection is not on the documents allowlist")
        return open_collection(collection, settings, lambda: self._recheck(settings))

    def collections(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Report configured names, not root paths, file counts, or cached availability."""
        settings = self._settings()
        return {
            **_provenance(),
            "collections": [
                {"name": item.name, "extensions": list(item.extensions)} for item in settings.collections
            ],
            "availability_checked": False,
            "limits": {
                "max_entries": settings.max_entries,
                "max_file_bytes": settings.max_file_bytes,
                "max_search_bytes": settings.max_search_bytes,
                "max_return_chars": settings.max_return_chars,
            },
        }

    def listing(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        name = args["collection"]
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        with self._source(settings, name) as source:
            candidates, skipped, examined = source.candidates()
            page = candidates[offset : offset + limit]
            for candidate in page:
                source.observed[candidate.name] = candidate.info
            return {
                **_provenance(),
                "collection": name,
                "notes": [
                    {"filename": item.name, "size_bytes": item.info.st_size, "untrusted_content": True}
                    for item in page
                ],
                "total": len(candidates),
                "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(candidates) else None,
                "entries_examined": examined,
                "skipped": dict(skipped),
                "listing_complete": True,
                "text_validated": False,
            }

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        name, query = args["collection"], args["query"]
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        budget = ReadBudget(settings.max_search_bytes)
        results: list[dict[str, Any]] = []
        matches, searched, remaining_text = 0, 0, settings.max_return_chars
        exhausted = False
        with self._source(settings, name) as source:
            candidates, skipped, examined = source.candidates()
            for candidate in candidates:
                try:
                    note = source.read(candidate.name, budget)
                except SearchBudgetExhausted:
                    exhausted = True
                    break
                except NoteUnavailable as exc:
                    skipped[exc.reason] += 1
                    continue
                searched += 1
                position = note.text.find(query)
                if position < 0:
                    continue
                matches += 1
                if matches <= offset or len(results) >= limit:
                    continue
                width = min(SNIPPET_CHARS, remaining_text)
                snippet = note.text[position : position + width]
                remaining_text -= len(snippet)
                results.append(
                    {
                        "filename": candidate.name,
                        "match_offset": position,
                        "snippet": snippet,
                        "snippet_truncated": position > 0 or position + len(snippet) < len(note.text),
                        "text_omitted": not snippet,
                        "untrusted_content": True,
                    }
                )
            return {
                **_provenance(),
                "collection": name,
                "results": results,
                "offset": offset,
                "next_offset": offset + len(results) if offset + len(results) < matches else None,
                "matches_in_scanned_files": matches,
                "files_searched": searched,
                "entries_examined": examined,
                "bytes_read": budget.consumed,
                "budget_exhausted": exhausted,
                "coverage_complete": not exhausted and not self._incomplete(skipped),
                "skipped": dict(skipped),
                "returned_text_chars": settings.max_return_chars - remaining_text,
                "text_truncated": any(item["snippet_truncated"] for item in results),
                "case_sensitive": True,
                "literal": True,
            }

    @staticmethod
    def _incomplete(skipped: Counter[str]) -> bool:
        return any(count for reason, count in skipped.items() if reason != "unsupported_name")

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        name, filename = args["collection"], args["filename"]
        offset = args.get("offset", 0)
        maximum = min(args.get("max_chars", 4000), settings.max_return_chars)
        with self._source(settings, name) as source:
            note = source.read(filename, ReadBudget(settings.max_file_bytes + 1))
            text = note.text[offset : offset + maximum]
            end = min(offset + len(text), len(note.text))
            has_more = end < len(note.text)
            return {
                **_provenance(),
                "collection": name,
                "filename": filename,
                "text": text,
                "size_bytes": note.size_bytes,
                "total_chars": len(note.text),
                "offset": offset,
                "next_offset": end if has_more else None,
                "has_more": has_more,
                "text_truncated": offset > 0 or has_more,
            }


def check_collection(collection: Collection, settings: Settings) -> None:
    """Check permissions and path components without enumerating or reading any note."""
    with open_collection(collection, settings, lambda: None):
        pass
