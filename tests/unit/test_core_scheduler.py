"""Bounded runner: worker limit, per-lane serialization, overruns, stuck jobs, and shutdown.

Jobs block on ``threading.Event`` objects and the scheduler reads a fake clock, so nothing here depends on
real elapsed time beyond short, bounded waits for worker threads to report back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from switchboard import execution
from switchboard.execution import Lanes
from switchboard.scheduler import LANE_BUSY, Scheduler
from tests.conftest import AppFactory

WAIT = 5.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def eventually(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT
    while not predicate():
        assert time.monotonic() < deadline, "a worker thread did not report back in time"
        time.sleep(0.005)


def blocking(release: threading.Event, started: list[str], name: str) -> Callable[[], None]:
    def job() -> None:
        started.append(name)
        release.wait(WAIT)

    return job


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make(clock: Clock, **options: Any) -> Scheduler:
    return Scheduler(clock, poll_seconds=0.01, **options)


def recorder(finished: list[str], name: str) -> Callable[[], None]:
    def job() -> None:
        finished.append(name)

    return job


def running(scheduler: Scheduler) -> list[str]:
    return [item["name"] for item in scheduler.status() if item["running"]]


def test_run_due_starts_jobs_on_workers_and_returns_without_waiting(clock: Clock) -> None:
    scheduler = make(clock)
    release = threading.Event()
    started: list[str] = []
    scheduler.add("mail.poll", 60, blocking(release, started, "mail.poll"))

    assert scheduler.run_due() == ["mail.poll"]

    assert running(scheduler) == ["mail.poll"]
    release.set()
    assert scheduler.wait(WAIT)
    eventually(lambda: running(scheduler) == [])
    [state] = scheduler.status()
    assert (state["runs"], state["failures"], state["last_error"]) == (1, 0, None)
    assert state["due_in_seconds"] == 60


def test_a_job_never_runs_twice_at_once_and_missed_runs_count_as_overruns(clock: Clock) -> None:
    scheduler = make(clock)
    release = threading.Event()
    started: list[str] = []
    scheduler.add("mail.poll", 60, blocking(release, started, "mail.poll"))
    scheduler.run_due()
    clock.advance(60)
    assert scheduler.run_due() == []
    clock.advance(60)
    assert scheduler.run_due() == []
    assert scheduler.status()[0]["overruns"] == 2
    release.set()
    eventually(lambda: running(scheduler) == [])
    assert started == ["mail.poll"]
    assert scheduler.jobs()[0].runs == 1


def test_jobs_sharing_a_lane_are_serialized_while_other_lanes_run(clock: Clock) -> None:
    scheduler = make(clock)
    releases = {name: threading.Event() for name in ("calendar.refresh", "mail.poll", "mail.prune")}
    started: list[str] = []
    for name, release in releases.items():
        scheduler.add(name, 60, blocking(release, started, name))

    assert scheduler.run_due() == ["calendar.refresh", "mail.poll"]
    assert scheduler.run_due() == []

    releases["mail.poll"].set()
    eventually(lambda: "mail.poll" not in running(scheduler))
    assert scheduler.run_due() == ["mail.prune"]
    releases["calendar.refresh"].set()
    releases["mail.prune"].set()
    eventually(lambda: running(scheduler) == [])
    assert sorted(started) == sorted(releases)


def test_an_explicit_lane_overrides_the_name_prefix(clock: Clock) -> None:
    scheduler = make(clock)
    release = threading.Event()
    started: list[str] = []
    scheduler.add("first.job", 60, blocking(release, started, "first.job"), lane="shared")
    scheduler.add("second.job", 60, blocking(release, started, "second.job"), lane="shared")
    assert scheduler.run_due() == ["first.job"]
    assert {item["name"]: item["lane"] for item in scheduler.status()} == {
        "first.job": "shared",
        "second.job": "shared",
    }
    release.set()
    eventually(lambda: running(scheduler) == [])


def test_the_worker_limit_bounds_concurrency(clock: Clock) -> None:
    scheduler = make(clock, workers=2)
    releases = {name: threading.Event() for name in ("a.poll", "b.poll", "c.poll")}
    started: list[str] = []
    for name, release in releases.items():
        scheduler.add(name, 60, blocking(release, started, name))

    assert scheduler.run_due() == ["a.poll", "b.poll"]
    releases["a.poll"].set()
    eventually(lambda: "a.poll" not in running(scheduler))
    assert scheduler.run_due() == ["c.poll"]
    for release in releases.values():
        release.set()
    eventually(lambda: running(scheduler) == [])


def test_failures_record_the_exception_type_and_the_duration(clock: Clock) -> None:
    scheduler = make(clock)

    def fail() -> None:
        clock.advance(7)
        raise ValueError("private provider detail")

    scheduler.add("shopping.prices", 60, fail)
    assert scheduler.run_all() == ["shopping.prices"]
    [state] = scheduler.status()
    assert (state["last_error"], state["failures"], state["runs"]) == ("ValueError", 1, 1)
    assert state["last_duration_seconds"] == 7
    assert "private" not in str(state)


def test_stuck_jobs_are_reported_and_never_restarted(clock: Clock) -> None:
    scheduler = make(clock, job_timeout_seconds=10)
    release = threading.Event()
    calls: list[str] = []

    def hang() -> None:
        calls.append("hang")
        clock.advance(11)
        release.wait(WAIT)

    scheduler.add("mail.poll", 60, hang)
    scheduler.run_due()
    eventually(lambda: scheduler.stuck() == ["mail.poll"])
    assert scheduler.status()[0]["stuck"] is True
    clock.advance(60)
    assert scheduler.run_due() == []
    assert scheduler.status()[0]["overruns"] == 1

    release.set()
    eventually(lambda: running(scheduler) == [])
    assert scheduler.stuck() == []
    assert calls == ["hang"]


def test_run_all_waits_for_every_job(clock: Clock) -> None:
    scheduler = make(clock)
    finished: list[str] = []
    for name in ("b.poll", "a.poll", "a.prune"):
        scheduler.add(name, 60, recorder(finished, name))

    assert sorted(scheduler.run_all()) == ["a.poll", "a.prune", "b.poll"]

    assert sorted(finished) == ["a.poll", "a.prune", "b.poll"]
    assert all(item["runs"] == 1 and not item["running"] for item in scheduler.status())
    assert scheduler.skipped() == []
    assert scheduler.stuck() == []


def test_run_all_leaves_a_stuck_job_behind_and_skips_its_lane(clock: Clock) -> None:
    scheduler = make(clock, job_timeout_seconds=10)
    release = threading.Event()
    calendar_done = threading.Event()
    finished: list[str] = []

    def refresh() -> None:
        finished.append("calendar.refresh")
        calendar_done.set()

    def hang() -> None:
        calendar_done.wait(WAIT)
        clock.advance(11)
        release.wait(WAIT)

    scheduler.add("mail.poll", 60, hang)
    scheduler.add("mail.prune", 60, recorder(finished, "mail.prune"))
    scheduler.add("calendar.refresh", 60, refresh)

    assert sorted(scheduler.run_all()) == ["calendar.refresh", "mail.poll"]

    assert finished == ["calendar.refresh"]
    eventually(lambda: scheduler.stuck() == ["mail.poll"])
    assert scheduler.skipped() == ["mail.prune"]
    release.set()
    eventually(lambda: running(scheduler) == [])


def test_shutdown_stops_scheduling_and_waits_for_running_jobs(clock: Clock) -> None:
    scheduler = make(clock)
    release = threading.Event()
    started: list[str] = []
    scheduler.add("mail.poll", 60, blocking(release, started, "mail.poll"))
    scheduler.add("calendar.refresh", 60, lambda: None, delay=120)
    scheduler.run_due()
    outcome: list[list[str]] = []
    done = threading.Event()

    def stop() -> None:
        outcome.append(scheduler.shutdown())
        done.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    assert not done.wait(0.05)
    release.set()
    assert done.wait(WAIT)
    stopper.join(WAIT)
    assert outcome == [[]]
    clock.advance(600)
    assert scheduler.run_due() == []
    assert scheduler.run_all() == []


def test_shutdown_abandons_jobs_past_their_deadline(clock: Clock) -> None:
    scheduler = make(clock, job_timeout_seconds=10)
    release = threading.Event()

    def hang() -> None:
        clock.advance(11)
        release.wait(WAIT)

    scheduler.add("mail.poll", 60, hang)
    scheduler.run_due()
    eventually(lambda: scheduler.stuck() == ["mail.poll"])
    try:
        assert scheduler.shutdown() == ["mail.poll"]
    finally:
        release.set()
    eventually(lambda: running(scheduler) == [])


def test_each_run_has_the_job_deadline_and_holds_its_lane(clock: Clock) -> None:
    lanes = Lanes()
    scheduler = make(clock, job_timeout_seconds=45)
    scheduler.lanes = lanes
    seen: list[tuple[str, float, bool]] = []

    def inspect() -> None:
        limit = execution.current()
        assert limit is not None
        probes: list[bool] = []

        def probe() -> None:
            with lanes.hold("mail", timeout=0) as held:
                probes.append(held)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(WAIT)
        seen.append((limit.label, limit.seconds, probes == [False]))

    scheduler.add("mail.poll", 60, inspect)
    scheduler.run_all()
    assert seen == [("job mail.poll", 45, True)]


def test_a_job_whose_lane_is_held_by_a_stuck_tool_does_not_run(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lanes = Lanes()
    scheduler = make(clock)
    scheduler.lanes = lanes
    ran: list[str] = []
    scheduler.add("mail.poll", 60, lambda: ran.append("mail.poll"))
    real = execution.monotonic
    with execution.deadline(1, "tool mail_send"), lanes.hold("mail"):
        monkeypatch.setattr(execution, "monotonic", lambda: real() + 3600)
        scheduler.run_all()
    assert ran == []
    assert scheduler.status()[0]["last_error"] == LANE_BUSY


def test_tool_executions_and_jobs_share_the_apps_lanes(make_app: AppFactory) -> None:
    app = make_app({"switchboard": {"job_workers": 2, "job_timeout_seconds": 60}})
    scheduler = app.ctx.scheduler
    assert scheduler.lanes is app.dispatcher.lanes
    assert (scheduler.workers, scheduler.job_timeout_seconds) == (2, 60)
    assert [job.lane for job in scheduler.jobs()] == ["core"]


def test_scheduling_rules_are_validated(clock: Clock) -> None:
    scheduler = make(clock)
    scheduler.add("mail.poll", 60, lambda: None)
    with pytest.raises(ValueError, match="impolite"):
        scheduler.add("mail.fast", 29, lambda: None)
    with pytest.raises(ValueError, match="already scheduled"):
        scheduler.add("mail.poll", 60, lambda: None)
    with pytest.raises(ValueError, match="lane"):
        scheduler.add("mail.other", 60, lambda: None, lane="")
    for options, message in (
        ({"workers": 0}, "workers must be between 1 and 64"),
        ({"workers": 65}, "workers must be between 1 and 64"),
        ({"job_timeout_seconds": 0}, "job_timeout_seconds must be positive"),
    ):
        with pytest.raises(ValueError, match=message):
            make(clock, **options)


def test_seconds_until_next_follows_the_earliest_job(clock: Clock) -> None:
    scheduler = make(clock)
    assert scheduler.seconds_until_next() == 60.0
    scheduler.add("mail.poll", 60, lambda: None, delay=90)
    scheduler.add("calendar.refresh", 60, lambda: None, delay=30)
    assert scheduler.seconds_until_next() == 30
    clock.advance(45)
    assert scheduler.seconds_until_next() == 0.0
