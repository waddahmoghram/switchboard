"""Local task revisions and one-shot reminder events commit together."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from switchboard.context import AppContext
from switchboard.db import Migration, transaction
from switchboard.errors import ToolError
from switchboard.util import iso, truncate

MIGRATIONS: list[Migration] = [
    (
        1,
        (
            "CREATE TABLE tasks_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL, "
            "list_name TEXT NOT NULL, title TEXT NOT NULL, notes TEXT NOT NULL, search_text TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK(status IN ('open', 'completed', 'cancelled')), "
            "due_at TEXT, remind_at TEXT, reminder_key TEXT, reminder_event_queued_at TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX tasks_reminders ON tasks_items(status, reminder_event_queued_at, remind_at)",
            "CREATE INDEX tasks_lists ON tasks_items(list_name, id)",
        ),
    )
]


def _item(conn: sqlite3.Connection, identifier: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks_items WHERE id = ?", (identifier,)).fetchone()
    if row is None:
        raise ToolError("no local task with that id")
    return dict(row)


def _public(item: dict[str, Any], *, preview: bool = False) -> dict[str, Any]:
    result = {key: value for key, value in item.items() if key not in {"reminder_key", "search_text"}}
    if preview:
        result["notes"] = truncate(item["notes"], 180)
        result["notes_truncated"] = len(item["notes"]) > 180
    result["untrusted_content"] = True
    result["reminder_delivery"] = (
        "event_queued"
        if item["reminder_event_queued_at"]
        else "not_queued"
        if item["remind_at"]
        else "not_scheduled"
    )
    return result


class TaskStore:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def create(self, fields: Mapping[str, Any], moment: datetime) -> dict[str, Any]:
        stamp = iso(moment)
        title, notes = fields["title"], fields.get("notes", "")
        remind_at = fields.get("remind_at")
        with self.ctx.db.session() as conn, transaction(conn):
            cursor = conn.execute(
                "INSERT INTO tasks_items "
                "(revision, list_name, title, notes, search_text, status, due_at, remind_at, reminder_key, "
                "created_at, updated_at) VALUES (1, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (
                    fields.get("list_name", "personal"),
                    title,
                    notes,
                    (title + "\n" + notes).casefold(),
                    fields.get("due_at"),
                    remind_at,
                    uuid.uuid4().hex if remind_at is not None else None,
                    stamp,
                    stamp,
                ),
            )
            identifier = cursor.lastrowid
            if identifier is None:
                raise ToolError("the task could not be assigned an identifier")
            item = _item(conn, identifier)
            self._changed(conn, item)
        return _public(item)

    def read(self, identifier: int) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            return _public(_item(conn, identifier))

    def search(
        self,
        *,
        status: str,
        query: str,
        list_name: str | None,
        after_id: int,
        limit: int,
        overdue: bool,
        moment: datetime,
    ) -> dict[str, Any]:
        with self.ctx.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks_items WHERE id > ? AND instr(search_text, ?) > 0 "
                "AND (? = 'all' OR status = ?) AND (? IS NULL OR list_name = ?) "
                "AND (? = 0 OR (status = 'open' AND due_at IS NOT NULL AND due_at <= ?)) "
                "ORDER BY id LIMIT ?",
                (
                    after_id,
                    query.casefold(),
                    status,
                    status,
                    list_name,
                    list_name,
                    overdue,
                    iso(moment),
                    limit + 1,
                ),
            ).fetchall()
        return {
            "tasks": [_public(dict(row), preview=True) for row in rows[:limit]],
            "truncated": len(rows) > limit,
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            "untrusted_content": True,
        }

    def update(
        self, identifier: int, revision: int, changes: Mapping[str, Any], moment: datetime
    ) -> dict[str, Any]:
        if not changes or set(changes) - {"list_name", "title", "notes", "status", "due_at", "remind_at"}:
            raise ToolError("provide at least one supported task field to change")
        with self.ctx.db.session() as conn, transaction(conn):
            current = _item(conn, identifier)
            if current["revision"] != revision:
                raise ToolError("the task changed; read its current revision before updating it")
            item = {**current, **changes}
            if item["remind_at"] != current["remind_at"]:
                item["reminder_key"] = uuid.uuid4().hex if item["remind_at"] is not None else None
                item["reminder_event_queued_at"] = None
            cursor = conn.execute(
                "UPDATE tasks_items SET revision = revision + 1, list_name = ?, title = ?, notes = ?, "
                "search_text = ?, status = ?, due_at = ?, remind_at = ?, reminder_key = ?, "
                "reminder_event_queued_at = ?, updated_at = ? WHERE id = ? AND revision = ?",
                (
                    item["list_name"],
                    item["title"],
                    item["notes"],
                    (item["title"] + "\n" + item["notes"]).casefold(),
                    item["status"],
                    item["due_at"],
                    item["remind_at"],
                    item["reminder_key"],
                    item["reminder_event_queued_at"],
                    iso(moment),
                    identifier,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolError("the task changed before the update could be saved")
            item = _item(conn, identifier)
            self._changed(conn, item)
        return _public(item)

    def delete(self, identifier: int, revision: int, moment: datetime) -> dict[str, Any]:
        with self.ctx.db.session() as conn, transaction(conn):
            item = _item(conn, identifier)
            if item["revision"] != revision:
                raise ToolError("the task changed; request deletion of its current revision instead")
            conn.execute("DELETE FROM tasks_items WHERE id = ? AND revision = ?", (identifier, revision))
            self.ctx.events.emit(
                "tasks",
                "tasks.deleted",
                {"id": identifier, "revision": revision, "deleted_at": iso(moment)},
                dedupe_key=f"tasks:deleted:{identifier}:{revision}",
                connection=conn,
            )
        return {"id": identifier, "revision": revision, "status": "deleted"}

    def queue_due(self, moment: datetime, maximum: int) -> int:
        stamp = iso(moment)
        with self.ctx.db.session() as conn, transaction(conn):
            rows = conn.execute(
                "SELECT id, revision, title, due_at, remind_at, reminder_key FROM tasks_items "
                "WHERE status = 'open' AND reminder_event_queued_at IS NULL AND remind_at <= ? "
                "ORDER BY remind_at, id LIMIT ?",
                (stamp, maximum),
            ).fetchall()
            for row in rows:
                self.ctx.events.emit(
                    "tasks",
                    "tasks.reminder_due",
                    {
                        "id": row["id"],
                        "revision": row["revision"],
                        "title": row["title"],
                        "due_at": row["due_at"],
                        "remind_at": row["remind_at"],
                        "untrusted_content": True,
                        "delivery": "event_queued",
                    },
                    dedupe_key=f"tasks:reminder:{row['id']}:{row['reminder_key']}",
                    connection=conn,
                )
                conn.execute(
                    "UPDATE tasks_items SET reminder_event_queued_at = ? WHERE id = ?",
                    (stamp, row["id"]),
                )
        return len(rows)

    def counts(self) -> dict[str, int]:
        with self.ctx.db.session() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks_items GROUP BY status").fetchall()
        counts = {"open": 0, "completed": 0, "cancelled": 0}
        counts.update({row["status"]: row["n"] for row in rows})
        return counts

    def _changed(self, conn: sqlite3.Connection, item: Mapping[str, Any]) -> None:
        self.ctx.events.emit(
            "tasks",
            "tasks.changed",
            {"id": item["id"], "revision": item["revision"], "status": item["status"]},
            dedupe_key=f"tasks:changed:{item['id']}:{item['revision']}",
            connection=conn,
        )
