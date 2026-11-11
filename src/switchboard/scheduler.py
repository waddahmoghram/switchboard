"""A tiny interval scheduler for polling jobs (mailbox checks, calendar refreshes, price checks)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: float
    func: Callable[[], None]
    next_run: float = 0.0
    last_error: str | None = None


class Scheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}

    def add(
        self, name: str, interval_seconds: float, func: Callable[[], None], *, delay: float = 0.0
    ) -> None:
        if interval_seconds < 30:
            raise ValueError(f"job {name}: polling more often than every 30 seconds is impolite to providers")
        if name in self._jobs:
            raise ValueError(f"job {name!r} is already scheduled")
        self._jobs[name] = Job(name, interval_seconds, func, next_run=self._clock() + delay)

    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.name)

    def run_due(self) -> list[str]:
        ran: list[str] = []
        for job in self.jobs():
            if job.next_run <= self._clock():
                self._run(job)
                ran.append(job.name)
        return ran

    def run_all(self) -> list[str]:
        for job in self.jobs():
            self._run(job)
        return [job.name for job in self.jobs()]

    def seconds_until_next(self) -> float:
        if not self._jobs:
            return 60.0
        return max(0.0, min(job.next_run for job in self._jobs.values()) - self._clock())

    def _run(self, job: Job) -> None:
        try:
            job.func()
            job.last_error = None
        except Exception as exc:
            log.exception("job %s failed", job.name)
            job.last_error = type(exc).__name__
        job.next_run = self._clock() + job.interval
