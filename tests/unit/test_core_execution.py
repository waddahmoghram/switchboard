"""Execution deadlines (a context variable) and per-connector lanes shared by jobs and tools."""

from __future__ import annotations

import math
import threading
import time

import pytest

from switchboard import execution
from switchboard.execution import Lanes, deadline, lane_of


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeMonotonic:
    fake = FakeMonotonic()
    monkeypatch.setattr(execution, "monotonic", fake)
    return fake


def test_a_deadline_is_scoped_to_its_block(clock: FakeMonotonic) -> None:
    assert execution.current() is None and execution.remaining() is None
    with deadline(10, "tool demo") as limit:
        assert execution.current() is limit
        assert (limit.at, limit.seconds, limit.label) == (110.0, 10, "tool demo")
        clock.now += 4
        assert execution.remaining() == 6.0 and not limit.expired()
        clock.now += 6
        assert limit.expired()
    assert execution.current() is None


def test_nesting_can_shorten_but_never_extend_a_deadline(clock: FakeMonotonic) -> None:
    with deadline(10, "job outer") as outer:
        with deadline(30, "tool inner") as longer:
            assert longer is outer
        with deadline(5, "tool inner") as shorter:
            assert shorter.label == "tool inner" and shorter.at == 105.0
            assert execution.current() is shorter
        assert execution.current() is outer


@pytest.mark.parametrize("seconds", [0, -1, math.inf, math.nan])
def test_a_deadline_needs_a_positive_finite_budget(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive"), deadline(seconds, "tool demo"):
        pass


@pytest.mark.parametrize(
    ("name", "lane"), [("mail.poll", "mail"), ("core.prune_events", "core"), ("standalone", "standalone")]
)
def test_the_default_lane_is_the_connector_prefix(name: str, lane: str) -> None:
    assert lane_of(name) == lane


def test_a_lane_is_reentrant_for_its_holder_and_exclusive_for_others() -> None:
    lanes = Lanes()
    seen: list[bool] = []

    def other_thread() -> None:
        with lanes.hold("mail", timeout=0) as held:
            seen.append(held)

    with lanes.hold("mail") as outer:
        with lanes.hold("mail", timeout=0) as inner:
            assert outer and inner
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(5)
        with lanes.hold("calendar", timeout=0) as unrelated:
            assert unrelated
    assert seen == [False]
    with lanes.hold("mail", timeout=0) as free:
        assert free


def test_a_waiter_gets_the_lane_once_the_holder_releases_it() -> None:
    lanes = Lanes()
    holding = threading.Event()
    release = threading.Event()
    results: list[bool] = []

    def holder() -> None:
        with lanes.hold("mail"):
            holding.set()
            release.wait(5)

    def waiter() -> None:
        with lanes.hold("mail", timeout=5) as held:
            results.append(held)

    first = threading.Thread(target=holder)
    first.start()
    assert holding.wait(5)
    second = threading.Thread(target=waiter)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert results == [True]


def test_waiters_give_up_on_a_holder_past_its_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    lanes = Lanes()
    holding = threading.Event()
    release = threading.Event()

    def stuck_job() -> None:
        with deadline(5, "job mail.poll"), lanes.hold("mail"):
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=stuck_job)
    worker.start()
    try:
        assert holding.wait(5)
        assert not lanes.stuck("mail")
        real = execution.monotonic
        monkeypatch.setattr(execution, "monotonic", lambda: real() + 3600)
        assert lanes.stuck("mail")
        started = time.monotonic()
        with lanes.hold("mail", timeout=60) as held:
            assert not held
        assert time.monotonic() - started < 5
    finally:
        release.set()
        worker.join(5)
    assert not lanes.stuck("mail")


def test_a_holder_without_a_deadline_is_never_considered_stuck() -> None:
    lanes = Lanes()
    with lanes.hold("core"):
        assert not lanes.stuck("core")
    assert not lanes.stuck("never-used")
