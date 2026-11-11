"""Approval queue for calls the policy marked ``ask``.

An approval binds the *exact* arguments the assistant proposed. Approving executes those stored
arguments, so the assistant cannot change a recipient or amount after you said yes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from switchboard.db import Database
from switchboard.util import canonical_json, iso_after, now_iso


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tool: str
    args: dict[str, Any]
    summary: str
    origin: str
    status: str
    created_at: str
    expires_at: str
    decided_at: str | None
    result: Any
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalStore:
    def __init__(self, db: Database, *, ttl_seconds: int = 3600) -> None:
        if ttl_seconds < 60:
            raise ValueError("approvals should stay open for at least a minute")
        self.db = db
        self.ttl_seconds = ttl_seconds

    def create(self, *, tool: str, args: dict[str, Any], summary: str, origin: str) -> ApprovalRequest:
        for _ in range(8):
            approval_id = secrets.token_hex(4)
            try:
                with self.db.session() as conn:
                    conn.execute(
                        "INSERT INTO approvals "
                        "(id, tool, args_json, summary, origin, status, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                        (
                            approval_id,
                            tool,
                            canonical_json(args),
                            summary,
                            origin,
                            now_iso(),
                            iso_after(self.ttl_seconds),
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            request = self.get(approval_id)
            if request is None:  # pragma: no cover - the row was just written
                raise RuntimeError("approval vanished after insert")
            return request
        raise RuntimeError("could not allocate a unique approval id")

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return None if row is None else _from_row(row)

    def list(self, status: str | None = "pending", limit: int = 50) -> list[ApprovalRequest]:
        query = "SELECT * FROM approvals"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.db.session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_from_row(row) for row in rows]

    def claim(self, approval_id: str) -> bool:
        """Atomically move a live pending request to ``approved``; False if someone else got there first."""
        with self.db.session() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'approved', decided_at = ? "
                "WHERE id = ? AND status = 'pending' AND expires_at > ?",
                (now_iso(), approval_id, now_iso()),
            )
            return cursor.rowcount == 1

    def deny(self, approval_id: str) -> bool:
        with self.db.session() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'denied', decided_at = ? WHERE id = ? AND status = 'pending'",
                (now_iso(), approval_id),
            )
            return cursor.rowcount == 1

    def finish(self, approval_id: str, *, ok: bool, result: Any, error: str | None) -> None:
        with self.db.session() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, result_json = ?, error = ? "
                "WHERE id = ? AND status = 'approved'",
                ("executed" if ok else "failed", canonical_json(result), error, approval_id),
            )

    def expire(self) -> int:
        with self.db.session() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
                (now_iso(),),
            )
            return cursor.rowcount


def _from_row(row: sqlite3.Row) -> ApprovalRequest:
    result_json = row["result_json"]
    return ApprovalRequest(
        id=row["id"],
        tool=row["tool"],
        args=json.loads(row["args_json"]),
        summary=row["summary"],
        origin=row["origin"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        result=None if result_json is None else json.loads(result_json),
        error=row["error"],
    )
