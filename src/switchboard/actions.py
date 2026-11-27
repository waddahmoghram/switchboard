"""Durable action journal: every non-read execution attempt, its outcome, and operator reconciliation.

The journal answers one question after a crash or an ambiguous failure: *might this external effect
already have happened?* An attempt is recorded ``in_flight`` before its handler runs. It finishes as
``succeeded`` (the handler returned), ``failed`` (the handler asserted the effect did not happen), or
``uncertain`` (unknown). An identical request (same tool and arguments) is refused while an earlier one
is in flight or uncertain, so nothing blindly repeats a possibly completed write. Only the operator
can reconcile an uncertain attempt. See ``contracts/README.md`` for the cross-engine rules.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from switchboard.db import Database, transaction
from switchboard.errors import SwitchboardError
from switchboard.util import canonical_json, iso, json_loads, now, printable, sha256_hex, truncate

STATES = ("in_flight", "succeeded", "failed", "uncertain", "reconciled_applied", "reconciled_not_applied")
OPEN_STATES = frozenset({"in_flight", "uncertain"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "reconciled_applied", "reconciled_not_applied"})
SWEEP_GRACE_SECONDS = 60
INTERRUPTED = "interrupted or exceeded its deadline; the outcome is unknown"
Finish = Literal["succeeded", "failed", "uncertain"]


class ActionBlockedError(SwitchboardError):
    """An identical earlier attempt is still in flight or has an uncertain outcome."""

    def __init__(self, action_id: str, state: str) -> None:
        if state == "in_flight":
            detail = "wait for it to finish"
        else:
            detail = (
                "its effect may or may not have happened; the user must check and run "
                f"'switchboard actions resolve {action_id}' before it can be repeated"
            )
        super().__init__(
            f"an identical earlier request is {state.replace('_', ' ')} (action {action_id}); {detail}"
        )
        self.action_id = action_id
        self.state = state


class ActionStateError(SwitchboardError):
    """A reconciliation request names a missing attempt or one that is not uncertain."""


@dataclass(frozen=True)
class ActionRecord:
    id: str
    tool: str
    args_digest: str
    args: dict[str, Any]
    origin: str
    approval_id: str | None
    state: str
    owner: str
    started_at: str
    deadline_at: str
    finished_at: str | None
    error: str | None
    resolved_at: str | None
    resolution_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        """Everything except the stored arguments, which can contain private message text."""
        result = self.to_dict()
        del result["args"]
        del result["owner"]
        return result


def args_digest(args: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(dict(args)))


def normalize_note(note: str) -> str:
    """The stripped note; :class:`ActionStateError` unless it has 1 to 500 printable characters."""
    cleaned = note.strip()
    if not 1 <= len(cleaned) <= 500 or not cleaned.isprintable():
        raise ActionStateError("a reconciliation note of 1 to 500 printable characters is required")
    return cleaned


class ActionJournal:
    def __init__(
        self, db: Database, *, owner: str | None = None, clock: Callable[[], datetime] = now
    ) -> None:
        self.db = db
        self.owner = owner or f"{os.getpid()}:{secrets.token_hex(8)}"
        self.clock = clock

    def begin(
        self,
        *,
        tool: str,
        args: Mapping[str, Any],
        origin: str,
        approval_id: str | None,
        timeout_seconds: int,
    ) -> ActionRecord:
        """Sweep expired attempts, refuse an identical open one, then record this attempt, atomically."""
        if not 1 <= timeout_seconds <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        moment = self.clock()
        digest = args_digest(args)
        action_id = secrets.token_hex(16)
        with self.db.session() as conn, transaction(conn):
            self._sweep(conn, moment)
            blocking = conn.execute(
                "SELECT id, state FROM action_journal WHERE tool = ? AND args_digest = ? "
                "AND state IN ('in_flight', 'uncertain') ORDER BY started_at, id LIMIT 1",
                (tool, digest),
            ).fetchone()
            if blocking is None:
                conn.execute(
                    "INSERT INTO action_journal (id, tool, args_digest, args_json, origin, approval_id, "
                    "state, owner, started_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?, 'in_flight', ?, ?, ?)",
                    (
                        action_id,
                        tool,
                        digest,
                        canonical_json(dict(args)),
                        origin,
                        approval_id,
                        self.owner,
                        iso(moment),
                        iso(moment + timedelta(seconds=timeout_seconds)),
                    ),
                )
                row = conn.execute("SELECT * FROM action_journal WHERE id = ?", (action_id,)).fetchone()
        # Refuse only after the commit: raising inside the transaction would roll back the sweep.
        if blocking is not None:
            raise ActionBlockedError(str(blocking["id"]), str(blocking["state"]))
        return _record(row)

    def finish(self, action_id: str, state: Finish, *, error: str | None = None) -> ActionRecord | None:
        """Record this owner's result; a swept attempt still accepts it until the operator reconciles."""
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError(f"cannot finish an action as {state!r}")
        message = None if error is None else printable(truncate(error, 500))
        with self.db.session() as conn, transaction(conn):
            conn.execute(
                "UPDATE action_journal SET state = ?, finished_at = ?, error = ? "
                "WHERE id = ? AND owner = ? AND state IN ('in_flight', 'uncertain') AND resolved_at IS NULL",
                (state, iso(self.clock()), message, action_id, self.owner),
            )
            row = conn.execute("SELECT * FROM action_journal WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else _record(row)

    def sweep(self) -> int:
        """Mark in-flight attempts past their deadline plus a grace period as uncertain."""
        with self.db.session() as conn, transaction(conn):
            return self._sweep(conn, self.clock())

    def get(self, action_id: str) -> ActionRecord | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM action_journal WHERE id = ?", (action_id,)).fetchone()
        return None if row is None else _record(row)

    def for_approval(self, approval_id: str) -> ActionRecord | None:
        """The newest attempt that executed the given approval, if any."""
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM action_journal WHERE approval_id = ? "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (approval_id,),
            ).fetchone()
        return None if row is None else _record(row)

    def recent(self, *, state: str | None = None, limit: int = 20) -> list[ActionRecord]:
        if state is not None and state not in STATES:
            raise ValueError(f"unknown action state {state!r}")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT * FROM action_journal WHERE (? IS NULL OR state = ?) "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (state, state, limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def reconcile(self, action_id: str, *, applied: bool, note: str) -> ActionRecord:
        """Operator decision for an uncertain attempt; this never repeats or reverses the external effect.

        Expired in-flight attempts are swept first, so an attempt whose process died can be reconciled.
        """
        cleaned = normalize_note(note)
        state = "reconciled_applied" if applied else "reconciled_not_applied"
        with self.db.session() as conn, transaction(conn):
            moment = self.clock()
            self._sweep(conn, moment)
            changed = conn.execute(
                "UPDATE action_journal SET state = ?, resolved_at = ?, resolution_note = ? "
                "WHERE id = ? AND state = 'uncertain'",
                (state, iso(moment), cleaned, action_id),
            ).rowcount
            row = conn.execute("SELECT * FROM action_journal WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise ActionStateError(f"no action {action_id}")
        if changed != 1:
            raise ActionStateError(
                f"action {action_id} is {row['state']}; only uncertain actions are reconciled"
            )
        return _record(row)

    def purge_terminal(self, *, older_than_days: int) -> int:
        """Delete finished or reconciled attempts older than the cutoff; open attempts are never purged."""
        if not 1 <= older_than_days <= 3650:
            raise ValueError("older_than_days must be between 1 and 3650")
        cutoff = iso(self.clock() - timedelta(days=older_than_days))
        with self.db.session() as conn, transaction(conn):
            return int(
                conn.execute(
                    "DELETE FROM action_journal WHERE state IN "
                    "('succeeded', 'failed', 'reconciled_applied', 'reconciled_not_applied') "
                    "AND COALESCE(resolved_at, finished_at, started_at) < ?",
                    (cutoff,),
                ).rowcount
            )

    def counts(self) -> dict[str, int]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM action_journal GROUP BY state").fetchall()
        result = dict.fromkeys(STATES, 0)
        result.update({str(row["state"]): int(row["n"]) for row in rows})
        return result

    def _sweep(self, conn: sqlite3.Connection, moment: datetime) -> int:
        cutoff = iso(moment - timedelta(seconds=SWEEP_GRACE_SECONDS))
        return int(
            conn.execute(
                "UPDATE action_journal SET state = 'uncertain', error = ? "
                "WHERE state = 'in_flight' AND deadline_at < ?",
                (INTERRUPTED, cutoff),
            ).rowcount
        )


def _record(row: sqlite3.Row) -> ActionRecord:
    args = json_loads(str(row["args_json"]))
    return ActionRecord(
        id=str(row["id"]),
        tool=str(row["tool"]),
        args_digest=str(row["args_digest"]),
        args=args if isinstance(args, dict) else {},
        origin=str(row["origin"]),
        approval_id=row["approval_id"],
        state=str(row["state"]),
        owner=str(row["owner"]),
        started_at=str(row["started_at"]),
        deadline_at=str(row["deadline_at"]),
        finished_at=row["finished_at"],
        error=row["error"],
        resolved_at=row["resolved_at"],
        resolution_note=row["resolution_note"],
    )
