"""The runner: ``run --once`` reporting and the long-running loop's graceful shutdown (in process)."""

from __future__ import annotations

import argparse
import signal
import threading
import time

import pytest

from switchboard import cli
from switchboard.cli import RunnerStop, cmd_run, serve
from switchboard.errors import ToolError
from switchboard.scheduler import Scheduler
from tests.conftest import AppFactory

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_run_once_names_failed_stuck_and_skipped_jobs(
    make_app: AppFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app()
    clock = Clock()
    scheduler = Scheduler(clock, job_timeout_seconds=10, lanes=app.dispatcher.lanes, poll_seconds=0.01)
    app.ctx.scheduler = scheduler
    release = threading.Event()
    others = {"other.poll", "third.poll"}

    def hang() -> None:
        give_up = time.monotonic() + 5
        while time.monotonic() < give_up and any(
            item["running"] for item in scheduler.status() if item["name"] in others
        ):
            time.sleep(0.005)
        clock.now += 11
        release.wait(5)

    def fail() -> None:
        raise ToolError("synthetic polling failure")

    scheduler.add("demo.poll", 30, hang)
    scheduler.add("demo.prune", 30, lambda: None)
    scheduler.add("other.poll", 30, fail)
    scheduler.add("third.poll", 30, lambda: None)
    try:
        assert cmd_run(argparse.Namespace(once=True), app) == 1
    finally:
        release.set()

    captured = capsys.readouterr()
    assert "ran 3 job(s)" in captured.out
    assert "failed jobs: other.poll" in captured.err
    assert "stuck jobs (still running past their deadline): demo.poll" in captured.err
    assert "jobs not started (their lane or every worker stayed busy): demo.prune" in captured.err


def test_a_clean_run_once_processes_what_the_jobs_emitted(
    make_app: AppFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app()

    def emit() -> None:
        app.ctx.events.emit("demo", "demo.happened", {"value": 1})

    app.ctx.scheduler.add("demo.poll", 30, emit)
    assert cmd_run(argparse.Namespace(once=True), app) == 0
    captured = capsys.readouterr()
    assert "ran 2 job(s); processed 1 event(s)" in captured.out
    assert captured.err == ""


def test_serve_stops_scheduling_after_a_stop_request_and_waits_for_running_jobs(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    stop = RunnerStop()
    release = threading.Event()
    finished = threading.Event()

    def job() -> None:
        release.wait(5)
        finished.set()

    app.ctx.scheduler.add("demo.poll", 30, job)
    tick = app.tick

    def tick_then_stop() -> None:
        tick()
        stop.requested = True
        release.set()

    monkeypatch.setattr(app, "tick", tick_then_stop)

    assert serve(app, stop) == []

    assert finished.is_set()
    assert app.ctx.scheduler.run_due() == []


def test_serve_abandons_jobs_stuck_past_their_deadline(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    clock = Clock()
    scheduler = Scheduler(clock, job_timeout_seconds=10, lanes=app.dispatcher.lanes, poll_seconds=0.01)
    app.ctx.scheduler = scheduler
    stop = RunnerStop()
    release = threading.Event()

    def hang() -> None:
        clock.now += 11
        release.wait(5)

    scheduler.add("demo.poll", 30, hang)
    tick = app.tick

    def tick_then_stop() -> None:
        tick()
        stop.requested = True

    monkeypatch.setattr(app, "tick", tick_then_stop)
    try:
        assert serve(app, stop) == ["demo.poll"]
    finally:
        release.set()


def test_the_first_signal_requests_a_stop_and_a_second_interrupts() -> None:
    stop = RunnerStop()
    stop(signal.SIGTERM, None)
    assert stop.requested
    with pytest.raises(KeyboardInterrupt):
        stop(signal.SIGINT, None)


def test_the_long_running_command_installs_and_restores_signal_handlers(
    make_app: AppFactory, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    seen: list[bool] = []

    def fake_serve(_app: object, stop: RunnerStop) -> list[str]:
        seen.append(signal.getsignal(signal.SIGINT) is stop and signal.getsignal(signal.SIGTERM) is stop)
        return ["demo.poll"]

    monkeypatch.setattr(cli, "serve", fake_serve)

    assert cmd_run(argparse.Namespace(once=False), app) == 1

    assert seen == [True]
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    assert "left running past their deadlines: demo.poll" in capsys.readouterr().err
