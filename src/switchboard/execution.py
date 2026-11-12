"""Execution deadlines and connector lanes shared by tool executions and polling jobs.

A *deadline* is a context variable installed around every tool execution (``tool_timeout_seconds``) and every
polling-job run (``job_timeout_seconds``). :class:`switchboard.httpclient.HttpClient` reads it: web requests
made inside the call get at most the remaining time and fail once it has passed. Nothing here interrupts
other code. Python threads cannot be killed, so a handler stuck outside the web client keeps running past
its deadline; the action journal then treats an unfinished attempt as interrupted.

A *lane* serializes one connector's code inside a process. In the runner, a polling job holds its lane
(by default the job name's prefix before the first dot, which is the connector name) and a tool execution
holds the lane named by its connector, so a rule-triggered tool never runs alongside the same connector's
poll. A lane whose holder is past its own deadline is treated as stuck: waiters give up instead of stalling.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

monotonic: Callable[[], float] = time.monotonic  # deadline arithmetic; tests replace it to control time

STUCK_GRACE_SECONDS = 1.0
_WAIT_SLICE_SECONDS = 0.25


@dataclass(frozen=True)
class Deadline:
    """An instant on the :func:`monotonic` clock, with the budget and the label it was created for."""

    at: float
    seconds: float
    label: str

    def remaining(self) -> float:
        """Seconds left; zero or negative once the deadline has passed."""
        return self.at - monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


_CURRENT: ContextVar[Deadline | None] = ContextVar("switchboard_deadline", default=None)


def current() -> Deadline | None:
    return _CURRENT.get()


def remaining() -> float | None:
    """Seconds left before the current deadline, or None when no deadline is installed."""
    deadline = _CURRENT.get()
    return None if deadline is None else deadline.remaining()


@contextmanager
def deadline(seconds: float, label: str) -> Iterator[Deadline]:
    """Install a deadline for the block; nesting may shorten the current deadline but never extends it."""
    if not (seconds > 0 and math.isfinite(seconds)):
        raise ValueError("a deadline needs a positive, finite number of seconds")
    proposed = Deadline(monotonic() + seconds, seconds, label)
    outer = _CURRENT.get()
    chosen = outer if outer is not None and outer.at <= proposed.at else proposed
    token = _CURRENT.set(chosen)
    try:
        yield chosen
    finally:
        _CURRENT.reset(token)


def lane_of(job_name: str) -> str:
    """The default lane of a job: its name up to the first dot, which is the connector's name."""
    return job_name.partition(".")[0]


@dataclass(frozen=True)
class _Holder:
    thread: int
    deadline_at: float | None


class Lanes:
    """Per-lane locks that the same thread may re-enter; shared by a process's scheduler and dispatcher."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, _Holder] = {}

    @contextmanager
    def hold(self, lane: str, *, timeout: float | None = None) -> Iterator[bool]:
        """Hold ``lane`` for the block; yields False, without holding it, if it could not be acquired.

        Waiting ends after ``timeout`` seconds (None waits indefinitely) or as soon as the current holder is
        more than :data:`STUCK_GRACE_SECONDS` past its own deadline.
        """
        me = threading.get_ident()
        with self._guard:
            lock = self._locks.get(lane)
            if lock is None:
                lock = self._locks[lane] = threading.Lock()
            holder = self._holders.get(lane)
            reentrant = holder is not None and holder.thread == me
        if reentrant:
            yield True
            return
        if not self._acquire(lane, lock, timeout):
            yield False
            return
        limit = current()
        try:
            with self._guard:
                self._holders[lane] = _Holder(me, None if limit is None else limit.at)
            yield True
        finally:
            with self._guard:
                self._holders.pop(lane, None)
            lock.release()

    def stuck(self, lane: str) -> bool:
        """True when the lane's holder is past its own deadline by more than the grace period."""
        with self._guard:
            holder = self._holders.get(lane)
            limit = None if holder is None else holder.deadline_at
        return limit is not None and monotonic() > limit + STUCK_GRACE_SECONDS

    def _acquire(self, lane: str, lock: threading.Lock, timeout: float | None) -> bool:
        end = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            left = _WAIT_SLICE_SECONDS if end is None else min(_WAIT_SLICE_SECONDS, end - time.monotonic())
            if left <= 0:
                return lock.acquire(blocking=False)
            if lock.acquire(timeout=left):
                return True
            if self.stuck(lane):
                return False
