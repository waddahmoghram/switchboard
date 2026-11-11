"""Immutable post bodies and durable, single-attempt publication claims."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Literal

from switchboard.connectors._archive import archive_migrations
from switchboard.connectors.linkedin.settings import NAME
from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import canonical_json, now_iso, sha256_hex, truncate

PublishState = Literal["PUBLISHED", "REFUSED", "UNKNOWN"]
MIGRATIONS: list[Migration] = [
    *archive_migrations(NAME),
    (
        2,
        (
            "CREATE TABLE linkedin_drafts ("
            "id TEXT PRIMARY KEY, text TEXT NOT NULL, visibility TEXT NOT NULL, author_urn TEXT NOT NULL, "
            "api_version TEXT NOT NULL, draft_hash TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TRIGGER linkedin_draft_immutable BEFORE UPDATE ON linkedin_drafts "
            "BEGIN SELECT RAISE(ABORT, 'LinkedIn draft content is immutable'); END",
            "CREATE TABLE linkedin_publish_attempts ("
            "draft_id TEXT PRIMARY KEY REFERENCES linkedin_drafts(id), "
            "state TEXT NOT NULL CHECK(state IN ('IN_FLIGHT', 'PUBLISHED', 'REFUSED', 'UNKNOWN')), "
            "started_at TEXT NOT NULL, finished_at TEXT, http_status INTEGER, post_urn TEXT, reason TEXT)",
            "CREATE UNIQUE INDEX linkedin_one_inflight ON linkedin_publish_attempts ((1)) "
            "WHERE state = 'IN_FLIGHT'",
        ),
    ),
]
_SELECT = (
    "SELECT d.*, a.state, a.started_at, a.finished_at, a.http_status, a.post_urn, a.reason "
    "FROM linkedin_drafts d LEFT JOIN linkedin_publish_attempts a ON a.draft_id = d.id"
)


def _arguments(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "draft_id": row["id"],
        "draft_hash": row["draft_hash"],
        "text": row["text"],
        "visibility": row["visibility"],
        "author_urn": row["author_urn"],
        "api_version": row["api_version"],
    }


def _digest(arguments: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({key: value for key, value in arguments.items() if key != "draft_hash"}))


def _view(row: sqlite3.Row, *, preview: bool) -> dict[str, Any]:
    arguments = _arguments(row)
    result = {
        **arguments,
        "created_at": row["created_at"],
        "state": row["state"] or "DRAFT",
        "preview_only": preview,
        "characters": len(row["text"]),
        "attempt": None,
    }
    if row["state"] is not None:
        result["attempt"] = {
            key: row[key]
            for key in ("state", "started_at", "finished_at", "http_status", "post_urn", "reason")
        }
    if preview:
        result["text"] = truncate(row["text"], 240)
    else:
        result["publish_args"] = arguments if row["author_urn"] else None
    return result


class DraftStore:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def create(self, text: str, visibility: str, author_urn: str, api_version: str) -> dict[str, Any]:
        arguments = {
            "draft_id": uuid.uuid4().hex,
            "text": text,
            "visibility": visibility,
            "author_urn": author_urn,
            "api_version": api_version,
        }
        arguments["draft_hash"] = _digest(arguments)
        with self.ctx.db.session() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO linkedin_drafts (id, text, visibility, author_urn, api_version, "
                "draft_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    arguments["draft_id"],
                    text,
                    visibility,
                    author_urn,
                    api_version,
                    arguments["draft_hash"],
                    now_iso(),
                ),
            )
            self.ctx.events.emit(
                NAME,
                "linkedin.draft_created",
                {
                    "draft_id": arguments["draft_id"],
                    "draft_hash": arguments["draft_hash"],
                    "author_urn": author_urn,
                    "visibility": visibility,
                    "characters": len(text),
                },
                dedupe_key=f"linkedin:draft:{arguments['draft_id']}",
                connection=conn,
            )
        return self.read(arguments["draft_id"])

    def read(self, draft_id: str) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            row = conn.execute(f"{_SELECT} WHERE d.id = ?", (draft_id,)).fetchone()
        if row is None:
            raise ToolError("no LinkedIn draft with that identifier")
        return _view(row, preview=False)

    def list(self, limit: int) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            rows = conn.execute(
                f"{_SELECT} ORDER BY d.created_at DESC, d.id DESC LIMIT ?", (limit + 1,)
            ).fetchall()
        return {"drafts": [_view(row, preview=True) for row in rows[:limit]], "truncated": len(rows) > limit}

    def claim(self, arguments: dict[str, Any], *, author_urn: str, api_version: str) -> dict[str, Any]:
        """Check exact approved content and atomically reserve the only in-flight publication."""
        with self.ctx.db.session() as conn, transaction(conn):
            row = conn.execute(
                "SELECT * FROM linkedin_drafts WHERE id = ?", (arguments["draft_id"],)
            ).fetchone()
            if row is None:
                raise ToolError("no LinkedIn draft with that identifier")
            expected = _arguments(row)
            if arguments != expected or expected["draft_hash"] != _digest(expected):
                raise ToolError("publish arguments must exactly match the immutable LinkedIn draft")
            if (
                not author_urn
                or expected["author_urn"] != author_urn
                or expected["api_version"] != api_version
            ):
                raise ToolError("the configured LinkedIn actor or version changed; create a new draft")
            attempt = conn.execute(
                "SELECT state FROM linkedin_publish_attempts WHERE draft_id = ?", (arguments["draft_id"],)
            ).fetchone()
            if attempt is not None:
                raise ToolError(
                    f"this LinkedIn draft already has a {attempt['state']} attempt; no retry is allowed"
                )
            inflight = conn.execute(
                "SELECT 1 FROM linkedin_publish_attempts WHERE state = 'IN_FLIGHT'"
            ).fetchone()
            if inflight is not None:
                raise ToolError("another LinkedIn publication is IN_FLIGHT; reconcile it before publishing")
            conn.execute(
                "INSERT INTO linkedin_publish_attempts (draft_id, state, started_at) "
                "VALUES (?, 'IN_FLIGHT', ?)",
                (arguments["draft_id"], now_iso()),
            )
        return expected

    def finish(
        self,
        draft_id: str,
        state: PublishState,
        *,
        http_status: int | None = None,
        post_urn: str | None = None,
        reason: str | None = None,
    ) -> None:
        with self.ctx.db.session() as conn, transaction(conn):
            cursor = conn.execute(
                "UPDATE linkedin_publish_attempts SET state = ?, finished_at = ?, http_status = ?, "
                "post_urn = ?, reason = ? WHERE draft_id = ? AND state = 'IN_FLIGHT'",
                (state, now_iso(), http_status, post_urn, reason, draft_id),
            )
            if cursor.rowcount != 1:
                raise ToolError("the LinkedIn publication claim is unavailable; reconcile delivery manually")
            if state == "PUBLISHED":
                row = conn.execute("SELECT * FROM linkedin_drafts WHERE id = ?", (draft_id,)).fetchone()
                if row is None:
                    raise ToolError("the LinkedIn draft is unavailable; reconcile delivery manually")
                self.ctx.events.emit(
                    NAME,
                    "linkedin.post_published",
                    {
                        "draft_id": draft_id,
                        "draft_hash": row["draft_hash"],
                        "author_urn": row["author_urn"],
                        "visibility": row["visibility"],
                        "post_urn": post_urn,
                    },
                    dedupe_key=f"linkedin:published:{draft_id}",
                    connection=conn,
                )
