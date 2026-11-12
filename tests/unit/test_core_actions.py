"""Action journal: state machine, sweeps, late results, the duplicate guard, reconciliation, and retention."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from switchboard.actions import (
    INTERRUPTED,
    STATES,
    SWEEP_GRACE_SECONDS,
    ActionBlockedError,
    ActionJournal,
    ActionStateError,
    args_digest,
    normalize_note,
)
from switchboard.db import CORE_MIGRATIONS, Database
from switchboard.util import iso

START = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ARGS: dict[str, Any] = {"to": "pat@example.com", "text": "hello"}


class Clock:
    def __init__(self) -> None:
        self.moment = START

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "journal.sqlite3")
    database.add_migrations("core", CORE_MIGRATIONS)
    database.migrate()
    return database


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def journal(db: Database, clock: Clock) -> ActionJournal:
    return ActionJournal(db, owner="runner", clock=clock)


def begin(journal: ActionJournal, args: dict[str, Any] | None = None, *, timeout: int = 120) -> str:
    record = journal.begin(
        tool="demo_send",
        args=ARGS if args is None else args,
        origin="assistant",
        approval_id=None,
        timeout_seconds=timeout,
    )
    return record.id


def state_of(journal: ActionJournal, action_id: str) -> str:
    record = journal.get(action_id)
    assert record is not None
    return record.state


def test_begin_records_an_in_flight_attempt_with_its_deadline(journal: ActionJournal) -> None:
    record = journal.begin(
        tool="demo_send", args=ARGS, origin="approval", approval_id="0a1b2c3d", timeout_seconds=30
    )
    assert len(record.id) == 32 and set(record.id) <= set("0123456789abcdef")
    assert (record.state, record.owner, record.origin, record.approval_id) == (
        "in_flight",
        "runner",
        "approval",
        "0a1b2c3d",
    )
    assert record.args == ARGS
    assert record.args_digest == args_digest({"text": "hello", "to": "pat@example.com"})
    assert record.started_at == iso(START)
    assert record.deadline_at == iso(START + timedelta(seconds=30))
    assert record.finished_at is None and record.error is None and record.resolved_at is None


@pytest.mark.parametrize("timeout", [0, 86_401])
def test_begin_validates_the_timeout(journal: ActionJournal, timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        begin(journal, timeout=timeout)


@pytest.mark.parametrize(
    ("state", "error"), [("succeeded", None), ("failed", "refused"), ("uncertain", "timed out")]
)
def test_finish_records_each_classification(
    journal: ActionJournal, clock: Clock, state: Any, error: str | None
) -> None:
    action_id = begin(journal)
    clock.advance(5)
    record = journal.finish(action_id, state, error=error)
    assert record is not None
    assert (record.state, record.error, record.finished_at) == (
        state,
        error,
        iso(START + timedelta(seconds=5)),
    )


@pytest.mark.parametrize("state", ["in_flight", "reconciled_applied", "reconciled_not_applied", "done"])
def test_finish_refuses_states_that_are_not_results(journal: ActionJournal, state: Any) -> None:
    action_id = begin(journal)
    with pytest.raises(ValueError, match="cannot finish"):
        journal.finish(action_id, state)
    assert state_of(journal, action_id) == "in_flight"


def test_finish_bounds_and_cleans_the_error_text(journal: ActionJournal) -> None:
    action_id = begin(journal)
    record = journal.finish(action_id, "failed", error="\x1b[31m" + "x" * 900)
    assert record is not None and record.error is not None
    assert len(record.error) <= 510 and "\x1b" not in record.error


def test_an_identical_open_attempt_blocks_but_other_requests_do_not(journal: ActionJournal) -> None:
    first = begin(journal)
    with pytest.raises(ActionBlockedError) as blocked:
        begin(journal, {"text": "hello", "to": "pat@example.com"})
    assert (blocked.value.action_id, blocked.value.state) == (first, "in_flight")
    assert first in str(blocked.value) and "in flight" in str(blocked.value)
    begin(journal, {**ARGS, "text": "a different message"})
    other_tool = journal.begin(
        tool="demo_other", args=ARGS, origin="human", approval_id=None, timeout_seconds=60
    )
    assert other_tool.state == "in_flight"


def test_uncertain_attempts_block_until_reconciled(journal: ActionJournal) -> None:
    action_id = begin(journal)
    journal.finish(action_id, "uncertain", error="connection reset after sending")
    with pytest.raises(ActionBlockedError) as blocked:
        begin(journal)
    assert blocked.value.state == "uncertain"
    assert f"switchboard actions resolve {action_id}" in str(blocked.value)
    journal.reconcile(action_id, applied=False, note="checked the outbox: nothing was sent")
    assert state_of(journal, begin(journal)) == "in_flight"


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_finished_attempts_do_not_block_a_repeat(journal: ActionJournal, state: Any) -> None:
    journal.finish(begin(journal), state)
    assert state_of(journal, begin(journal)) == "in_flight"


def test_sweep_waits_for_the_deadline_plus_the_grace_period(journal: ActionJournal, clock: Clock) -> None:
    action_id = begin(journal, timeout=10)
    clock.advance(10 + SWEEP_GRACE_SECONDS)
    assert journal.sweep() == 0
    assert state_of(journal, action_id) == "in_flight"
    clock.advance(1)
    assert journal.sweep() == 1
    record = journal.get(action_id)
    assert record is not None and (record.state, record.error) == ("uncertain", INTERRUPTED)
    assert journal.sweep() == 0


def test_begin_sweeps_before_its_duplicate_check(journal: ActionJournal, clock: Clock) -> None:
    abandoned = begin(journal, timeout=10)
    clock.advance(10 + SWEEP_GRACE_SECONDS + 1)
    with pytest.raises(ActionBlockedError) as blocked:
        begin(journal)
    assert (blocked.value.action_id, blocked.value.state) == (abandoned, "uncertain")


def test_the_owner_may_record_a_late_result_after_a_sweep(db: Database, clock: Clock) -> None:
    runner = ActionJournal(db, owner="runner", clock=clock)
    other = ActionJournal(db, owner="assistant-server", clock=clock)
    action_id = begin(runner, timeout=5)
    clock.advance(5 + SWEEP_GRACE_SECONDS + 1)
    assert other.sweep() == 1
    unchanged = other.finish(action_id, "succeeded")
    assert unchanged is not None and unchanged.state == "uncertain"
    late = runner.finish(action_id, "succeeded")
    assert late is not None and (late.state, late.error) == ("succeeded", None)


def test_a_late_result_never_overrides_the_operator(db: Database, clock: Clock) -> None:
    runner = ActionJournal(db, owner="runner", clock=clock)
    operator = ActionJournal(db, owner="operator", clock=clock)
    action_id = begin(runner, timeout=5)
    clock.advance(5 + SWEEP_GRACE_SECONDS + 1)
    operator.reconcile(action_id, applied=False, note="the device log shows no change")
    record = runner.finish(action_id, "succeeded")
    assert record is not None and record.state == "reconciled_not_applied"


def test_reconcile_records_the_decision_and_the_note(journal: ActionJournal, clock: Clock) -> None:
    action_id = begin(journal)
    journal.finish(action_id, "uncertain", error="timeout")
    clock.advance(30)
    record = journal.reconcile(action_id, applied=True, note="  saw the message in the chat  ")
    assert (record.state, record.resolution_note) == ("reconciled_applied", "saw the message in the chat")
    assert record.resolved_at == iso(START + timedelta(seconds=30))
    with pytest.raises(ActionStateError, match="reconciled_applied"):
        journal.reconcile(action_id, applied=False, note="changed my mind")


@pytest.mark.parametrize("state", ["in_flight", "succeeded", "failed"])
def test_only_uncertain_attempts_can_be_reconciled(journal: ActionJournal, state: str) -> None:
    action_id = begin(journal)
    if state != "in_flight":
        journal.finish(action_id, "succeeded" if state == "succeeded" else "failed")
    with pytest.raises(ActionStateError, match=state):
        journal.reconcile(action_id, applied=True, note="checked")
    assert state_of(journal, action_id) == state


def test_reconciling_a_missing_attempt_fails(journal: ActionJournal) -> None:
    with pytest.raises(ActionStateError, match="no action"):
        journal.reconcile("0" * 32, applied=True, note="checked")


def test_reconcile_sweeps_an_abandoned_attempt_first(journal: ActionJournal, clock: Clock) -> None:
    action_id = begin(journal, timeout=5)
    clock.advance(5 + SWEEP_GRACE_SECONDS + 1)
    assert journal.reconcile(action_id, applied=False, note="the process died first").state == (
        "reconciled_not_applied"
    )


@pytest.mark.parametrize("note", ["", "   ", "x" * 501, "bell\x07", "line\nbreak"])
def test_reconciliation_notes_must_be_short_printable_text(journal: ActionJournal, note: str) -> None:
    action_id = begin(journal)
    journal.finish(action_id, "uncertain")
    with pytest.raises(ActionStateError, match="1 to 500 printable"):
        journal.reconcile(action_id, applied=True, note=note)
    with pytest.raises(ActionStateError):
        normalize_note(note)
    assert state_of(journal, action_id) == "uncertain"


def test_purge_removes_only_old_closed_attempts(journal: ActionJournal, clock: Clock) -> None:
    kept_open = [begin(journal, {"n": 1}), begin(journal, {"n": 2})]
    journal.finish(kept_open[1], "uncertain")
    closed = [begin(journal, {"n": index}) for index in range(3, 7)]
    journal.finish(closed[0], "succeeded")
    journal.finish(closed[1], "failed")
    journal.finish(closed[2], "uncertain")
    journal.reconcile(closed[2], applied=True, note="checked")
    journal.finish(closed[3], "uncertain")
    journal.reconcile(closed[3], applied=False, note="checked")
    recent = begin(journal, {"n": 7})
    clock.advance(timedelta(days=200).total_seconds())
    journal.finish(recent, "succeeded")

    assert journal.purge_terminal(older_than_days=180) == 4

    assert [state_of(journal, action_id) for action_id in kept_open] == ["in_flight", "uncertain"]
    assert all(journal.get(action_id) is None for action_id in closed)
    assert state_of(journal, recent) == "succeeded"


@pytest.mark.parametrize("days", [0, 3651])
def test_purge_validates_its_cutoff(journal: ActionJournal, days: int) -> None:
    with pytest.raises(ValueError, match="older_than_days"):
        journal.purge_terminal(older_than_days=days)


def test_recent_counts_and_public_views(journal: ActionJournal, clock: Clock) -> None:
    first = begin(journal, {"n": 1})
    clock.advance(1)
    second = begin(journal, {"n": 2})
    journal.finish(second, "failed", error="refused")
    assert [record.id for record in journal.recent()] == [second, first]
    assert [record.id for record in journal.recent(state="failed")] == [second]
    assert [record.id for record in journal.recent(limit=1)] == [second]
    public = journal.recent()[0].public()
    assert "args" not in public and "owner" not in public
    assert public["id"] == second and public["state"] == "failed"
    counts = journal.counts()
    assert set(counts) == set(STATES)
    assert (counts["in_flight"], counts["failed"], counts["uncertain"]) == (1, 1, 0)


@pytest.mark.parametrize(("state", "limit"), [("done", 10), (None, 0), (None, 501)])
def test_recent_validates_its_filters(journal: ActionJournal, state: str | None, limit: int) -> None:
    with pytest.raises(ValueError):
        journal.recent(state=state, limit=limit)


def test_for_approval_returns_the_newest_attempt_for_that_approval(
    journal: ActionJournal, clock: Clock
) -> None:
    assert journal.for_approval("0a1b2c3d") is None
    older = journal.begin(
        tool="demo_send", args={"n": 1}, origin="approval", approval_id="0a1b2c3d", timeout_seconds=60
    )
    clock.advance(1)
    newer = journal.begin(
        tool="demo_send", args={"n": 2}, origin="approval", approval_id="0a1b2c3d", timeout_seconds=60
    )
    found = journal.for_approval("0a1b2c3d")
    assert found is not None and found.id == newer.id != older.id


def test_concurrent_identical_attempts_admit_exactly_one(db: Database, clock: Clock) -> None:
    journals = [ActionJournal(db, owner=f"process-{index}", clock=clock) for index in range(2)]
    barrier = threading.Barrier(2)

    def attempt(journal: ActionJournal) -> str:
        barrier.wait(timeout=5)
        try:
            begin(journal)
        except ActionBlockedError:
            return "blocked"
        return "recorded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, journals))
    assert results == ["blocked", "recorded"]
