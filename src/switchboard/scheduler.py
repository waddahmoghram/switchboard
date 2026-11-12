"""Polling jobs (mailbox checks, calendar refreshes, price checks) on a small pool of worker threads.

Scheduling rules:

* Each job runs at a fixed interval measured from the start of its previous run, and never twice at once.
  A run that is still going when the next one is due counts as an *overrun*; the missed run is skipped.
* Jobs in the same *lane* never overlap. The default lane is the job name's prefix before the first dot,
  which is the connector's name. The dispatcher takes the same lanes for tool executions, so in the runner
  a rule-triggered tool never runs alongside its own connector's poll.
* At most ``workers`` jobs run at once (``[switchboard] job_workers``).
* Every run has a deadline of ``job_timeout_seconds`` for its web requests (:mod:`switchboard.execution`).
  Python threads cannot be killed: a run past its deadline keeps its worker and its lane, is reported as
  *stuck*, and its job does not start again until that run returns.

Job state is guarded by one lock, so the status methods can be called from any thread. Worker threads
are daemon threads: a process may exit while a stuck run is still going.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from switchboard import execution
from switchboard.execution import Lanes, lane_of

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 4
DEFAULT_JOB_TIMEOUT_SECONDS = 300
LANE_BUSY = "LaneBusy"


@dataclass
class Job:
    name: str
    interval: float
    func: Callable[[], None]
    next_run: float = 0.0
    last_error: str | None = None
    lane: str = ""
    running_since: float | None = None
    last_duration: float | None = None
    runs: int = 0
    failures: int = 0
    overruns: int = 0
    stuck_reported: bool = False


class Scheduler:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        workers: int = DEFAULT_WORKERS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        lanes: Lanes | None = None,
        poll_seconds: float = 0.5,
    ) -> None:
        if not 1 <= workers <= 64:
            raise ValueError("workers must be between 1 and 64")
        if not job_timeout_seconds > 0:
            raise ValueError("job_timeout_seconds must be positive")
        if not poll_seconds > 0:
            raise ValueError("poll_seconds must be positive")
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self.workers = workers
        self.job_timeout_seconds = job_timeout_seconds
        self.lanes = lanes if lanes is not None else Lanes()
        self.poll_seconds = poll_seconds
        self._changed = threading.Condition(threading.Lock())
        self._threads: dict[str, threading.Thread] = {}
        self._busy_lanes: set[str] = set()
        self._finished = threading.Event()
        self._closed = False
        self._skipped: list[str] = []

    def add(
        self,
        name: str,
        interval_seconds: float,
        func: Callable[[], None],
        *,
        delay: float = 0.0,
        lane: str | None = None,
    ) -> None:
        """Schedule ``func`` every ``interval_seconds``; ``lane`` defaults to the name's connector prefix."""
        if interval_seconds < 30:
            raise ValueError(f"job {name}: polling more often than every 30 seconds is impolite to providers")
        chosen = lane_of(name) if lane is None else lane
        if not chosen:
            raise ValueError(f"job {name}: the lane must be a non-empty name")
        with self._changed:
            if name in self._jobs:
                raise ValueError(f"job {name!r} is already scheduled")
            self._jobs[name] = Job(name, interval_seconds, func, next_run=self._clock() + delay, lane=chosen)

    def jobs(self) -> list[Job]:
        with self._changed:
            return sorted(self._jobs.values(), key=lambda job: job.name)

    def run_due(self) -> list[str]:
        """Start every due job that has a free worker and a free lane; returns the names started.

        This does not wait. A due job whose previous run is still going is counted as an overrun and its
        next run moves one interval ahead; a due job without a free worker or lane starts on a later call.
        """
        started: list[str] = []
        with self._changed:
            if self._closed:
                return started
            moment = self._clock()
            for job in sorted(self._jobs.values(), key=lambda item: (item.next_run, item.name)):
                if job.running_since is not None:
                    self._report_stuck(job, moment)
                    if job.next_run <= moment:
                        job.overruns += 1
                        job.next_run = moment + job.interval
                    continue
                if job.next_run <= moment and self._can_start(job):
                    self._start(job, moment)
                    started.append(job.name)
        return started

    def run_all(self) -> list[str]:
        """Run every job once on the workers and wait, bounded by each run's deadline; returns those started.

        A run still going after ``job_timeout_seconds`` is left behind (it cannot be stopped) and shows as
        stuck. A job whose lane stays held by a stuck run is not started; :meth:`skipped` lists it.
        """
        with self._changed:
            waiting = sorted(self._jobs.values(), key=lambda job: job.name)
            started: list[Job] = []
            while True:
                moment = self._clock()
                for job in list(waiting):
                    if self._can_start(job):
                        self._start(job, moment)
                        waiting.remove(job)
                        started.append(job)
                if not any(
                    job.running_since is not None and not self._is_stuck(job, moment)
                    for job in self._jobs.values()
                ):
                    break
                self._changed.wait(self.poll_seconds)
            for job in self._jobs.values():
                self._report_stuck(job, self._clock())
            self._skipped = [job.name for job in waiting]
        return [job.name for job in started]

    def skipped(self) -> list[str]:
        """Jobs the last :meth:`run_all` could not start because their lane or every worker stayed stuck."""
        with self._changed:
            return list(self._skipped)

    def stuck(self) -> list[str]:
        """Names of jobs whose current run is past its deadline."""
        with self._changed:
            moment = self._clock()
            return sorted(job.name for job in self._jobs.values() if self._is_stuck(job, moment))

    def status(self) -> list[dict[str, Any]]:
        """A snapshot of every job: running or stuck, last error and duration, runs, failures, overruns."""
        with self._changed:
            moment = self._clock()
            return [self._describe(job, moment) for job in sorted(self._jobs.values(), key=lambda j: j.name)]

    def seconds_until_next(self) -> float:
        with self._changed:
            if not self._jobs:
                return 60.0
            return max(0.0, min(job.next_run for job in self._jobs.values()) - self._clock())

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds, waking early when a run finishes; True if one did."""
        finished = self._finished.wait(max(0.0, timeout))
        self._finished.clear()
        return finished

    def shutdown(self) -> list[str]:
        """Stop starting jobs, then wait until every run has finished or passed its deadline.

        Returns the names of jobs still running (stuck); their daemon threads end with the process.
        """
        with self._changed:
            self._closed = True
            while True:
                moment = self._clock()
                running = [job for job in self._jobs.values() if job.running_since is not None]
                if all(self._is_stuck(job, moment) for job in running):
                    for job in running:
                        self._report_stuck(job, moment)
                    return sorted(job.name for job in running)
                self._changed.wait(self.poll_seconds)

    def _can_start(self, job: Job) -> bool:
        return (
            not self._closed
            and job.running_since is None
            and job.lane not in self._busy_lanes
            and len(self._threads) < self.workers
        )

    def _is_stuck(self, job: Job, moment: float) -> bool:
        return job.running_since is not None and moment - job.running_since > self.job_timeout_seconds

    def _start(self, job: Job, moment: float) -> None:
        job.running_since = moment
        job.next_run = moment + job.interval
        job.stuck_reported = False
        self._busy_lanes.add(job.lane)
        thread = threading.Thread(
            target=self._work, args=(job,), name=f"switchboard-job-{job.name}", daemon=True
        )
        self._threads[job.name] = thread
        thread.start()

    def _work(self, job: Job) -> None:
        error: str | None = "Interrupted"
        try:
            with (
                execution.deadline(self.job_timeout_seconds, f"job {job.name}") as limit,
                self.lanes.hold(job.lane, timeout=limit.remaining()) as held,
            ):
                if held:
                    job.func()
                    error = None
                else:
                    error = LANE_BUSY
                    log.warning("job %s did not run: lane %s stayed busy", job.name, job.lane)
        except Exception as exc:
            log.exception("job %s failed", job.name)
            error = type(exc).__name__
        finally:
            self._finish(job, error)

    def _finish(self, job: Job, error: str | None) -> None:
        with self._changed:
            moment = self._clock()
            started = job.running_since if job.running_since is not None else moment
            if self._is_stuck(job, moment):
                log.warning(
                    "job %s returned after %.0f seconds, past its deadline", job.name, moment - started
                )
            job.running_since = None
            job.last_duration = moment - started
            job.last_error = error
            job.runs += 1
            if error is not None:
                job.failures += 1
            self._threads.pop(job.name, None)
            self._busy_lanes.discard(job.lane)
            self._changed.notify_all()
        self._finished.set()

    def _report_stuck(self, job: Job, moment: float) -> None:
        if self._is_stuck(job, moment) and not job.stuck_reported:
            job.stuck_reported = True
            log.warning(
                "job %s has run for more than %s seconds and cannot be stopped; it keeps lane %s busy",
                job.name,
                f"{self.job_timeout_seconds:g}",
                job.lane,
            )

    def _describe(self, job: Job, moment: float) -> dict[str, Any]:
        running_for = None if job.running_since is None else moment - job.running_since
        return {
            "name": job.name,
            "lane": job.lane,
            "interval_seconds": job.interval,
            "running": job.running_since is not None,
            "running_seconds": running_for,
            "stuck": self._is_stuck(job, moment),
            "due_in_seconds": max(0.0, job.next_run - moment),
            "runs": job.runs,
            "failures": job.failures,
            "overruns": job.overruns,
            "last_error": job.last_error,
            "last_duration_seconds": job.last_duration,
        }
