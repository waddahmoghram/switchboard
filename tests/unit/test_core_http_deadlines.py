"""HTTP deadlines, bounded public-only name resolution, and opt-in idempotent retries. No network is used."""

from __future__ import annotations

import http.client
import io
import socket
import threading
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from typing import Any

import pytest

from switchboard import execution, httpclient
from switchboard.errors import HttpError
from switchboard.httpclient import (
    MAX_RETRY_AFTER_SECONDS,
    HttpClient,
    TransientHttpError,
    backoff_seconds,
    parse_retry_after,
    request_with_retries,
    require_public_host,
)
from tests.fakes import FakeHttp, RecordedRequest

URL = "https://api.example.com/items"


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


class Response:
    """An urllib-style response whose ``read1`` hands out one piece per call."""

    def __init__(self, pieces: list[bytes], *, on_read: Callable[[], None] | None = None) -> None:
        self.pieces = list(pieces)
        self.on_read = on_read
        self.read_calls = 0
        self.status = 200
        self.headers = Message()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.read_calls += 1
        body = b"".join(self.pieces)
        self.pieces = []
        return body[:limit]

    def read1(self, limit: int) -> bytes:
        if self.on_read is not None:
            self.on_read()
        return self.pieces.pop(0)[:limit] if self.pieces else b""

    def geturl(self) -> str:
        return URL


class Opener:
    """Replaces ``OpenerDirector.open`` and records the socket timeout of every attempt."""

    def __init__(
        self,
        replies: list[Any],
        *,
        before: Callable[[], None] | None = None,
    ) -> None:
        self.replies = replies
        self.before = before
        self.timeouts: list[float | None] = []

    def __call__(self, _request: object, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)
        if self.before is not None:
            self.before()
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply() if callable(reply) else reply


def client_with(monkeypatch: pytest.MonkeyPatch, opener: Opener, **options: Any) -> HttpClient:
    client = HttpClient(**options)
    monkeypatch.setattr(client._opener, "open", opener)
    return client


def test_the_socket_timeout_is_clamped_to_the_time_left(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = Opener([lambda: Response([b"ok"])])
    client = client_with(monkeypatch, opener, timeout=20)
    client.request("GET", URL)
    with execution.deadline(5, "tool demo_read"):
        clock.now += 1
        client.request("GET", URL)
        client.request("GET", URL, timeout=2)
    assert opener.timeouts == [20, 4.0, 2]


def test_nothing_is_sent_after_the_deadline(clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch) -> None:
    opener = Opener([lambda: Response([b"ok"])])
    client = client_with(monkeypatch, opener)
    with execution.deadline(5, "tool demo_write"):
        clock.now += 5
        with pytest.raises(HttpError, match="not sent") as refused:
            client.request("POST", URL, body=b"{}")
    assert "tool demo_write" in str(refused.value)
    assert not isinstance(refused.value, TransientHttpError)
    assert opener.timeouts == []


def test_a_slowly_arriving_body_fails_once_the_deadline_passes(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow() -> None:
        clock.now += 3

    response = Response([b"a" * 10, b"b" * 10, b"c" * 10], on_read=slow)
    client = client_with(monkeypatch, Opener([response]))
    with execution.deadline(5, "job calendar.refresh"), pytest.raises(HttpError, match="mid-response"):
        client.request("GET", URL)
    assert response.pieces == [b"c" * 10]


def test_a_response_that_arrives_after_the_deadline_fails(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    def late() -> None:
        clock.now += 10

    client = client_with(monkeypatch, Opener([lambda: Response([b"ok"])], before=late))
    with execution.deadline(5, "tool demo_write"), pytest.raises(HttpError, match="mid-response"):
        client.request("POST", URL, body=b"{}")


def test_without_a_deadline_the_body_is_read_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response([b"one", b"two"])
    client = client_with(monkeypatch, Opener([response]))
    assert client.request("GET", URL).body == b"onetwo"
    assert response.read_calls == 1


def test_within_a_deadline_the_body_is_read_one_receive_at_a_time(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = Response([b"one", b"two"])
    client = client_with(monkeypatch, Opener([response]))
    with execution.deadline(5, "tool demo_read"):
        assert client.request("GET", URL).body == b"onetwo"
    assert response.read_calls == 0


def test_the_size_limit_still_applies_to_piecewise_reads(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = client_with(monkeypatch, Opener([Response([b"x" * 6, b"y" * 6])]))
    with execution.deadline(5, "tool demo_read"), pytest.raises(HttpError, match="exceeded 8 bytes"):
        client.request("GET", URL, max_bytes=8)


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ConnectionRefusedError("refused")),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_network_failures_are_transient(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    client = client_with(monkeypatch, Opener([error]))
    with pytest.raises(TransientHttpError, match=r"api\.example\.com"):
        client.request("GET", URL)


def test_error_statuses_are_returned_with_their_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = Message()
    headers["Retry-After"] = "3"
    error = urllib.error.HTTPError(URL, 503, "Service Unavailable", headers, io.BytesIO(b"busy"))
    client = client_with(monkeypatch, Opener([error]))
    response = client.request("GET", URL)
    assert (response.status, response.body, response.headers["retry-after"]) == (503, b"busy", "3")


def test_public_only_name_resolution_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    try:
        with pytest.raises(TransientHttpError, match="took longer"):
            httpclient._public_addresses("shop.example.com", 443, 0.05)
    finally:
        release.set()


def test_resolution_errors_from_the_lookup_thread_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(HttpError, match="cannot resolve") as raised:
        httpclient._public_addresses("nowhere.example.invalid", 443, 5)
    assert not isinstance(raised.value, TransientHttpError)


def test_a_public_host_check_after_the_deadline_does_no_lookup(
    clock: FakeMonotonic, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("a lookup was attempted")

    monkeypatch.setattr(socket, "getaddrinfo", never)
    with execution.deadline(5, "job shopping.prices"):
        clock.now += 6
        with pytest.raises(HttpError, match="not sent"):
            require_public_host("https://shop.example.com/")


def test_get_retries_transient_statuses_with_jittered_backoff(fake_http: FakeHttp) -> None:
    fake_http.add("GET", URL, status=503)
    fake_http.add("GET", URL, status=502)
    fake_http.add("GET", URL, body="fine")
    sleeps: list[float] = []

    response = request_with_retries(fake_http, "GET", URL, retries=3, sleep=sleeps.append)

    assert (response.status, response.body) == (200, b"fine")
    assert len(fake_http.requests) == 3
    assert 0.25 <= sleeps[0] <= 0.5
    assert 0.5 <= sleeps[1] <= 1.0


def test_a_short_retry_after_is_honoured(fake_http: FakeHttp) -> None:
    fake_http.add("GET", URL, status=429, headers={"Retry-After": "2"})
    fake_http.add("GET", URL, body="fine")
    sleeps: list[float] = []
    assert request_with_retries(fake_http, "GET", URL, retries=1, sleep=sleeps.append).status == 200
    assert sleeps == [2.0]


@pytest.mark.parametrize("retry_after", ["120", str(int(MAX_RETRY_AFTER_SECONDS) + 1)])
def test_a_long_retry_after_returns_the_response_without_waiting(
    fake_http: FakeHttp, retry_after: str
) -> None:
    fake_http.add("GET", URL, status=503, headers={"Retry-After": retry_after})
    sleeps: list[float] = []
    assert request_with_retries(fake_http, "GET", URL, retries=3, sleep=sleeps.append).status == 503
    assert sleeps == []
    assert len(fake_http.requests) == 1


def test_a_wait_is_made_only_when_it_fits_the_deadline(clock: FakeMonotonic, fake_http: FakeHttp) -> None:
    fake_http.add("GET", URL, status=503, headers={"Retry-After": "5"})
    fake_http.add("GET", URL, status=503, headers={"Retry-After": "5"})
    fake_http.add("GET", URL, body="fine")
    sleeps: list[float] = []
    with execution.deadline(5.5, "tool demo_read"):
        assert request_with_retries(fake_http, "GET", URL, retries=3, sleep=sleeps.append).status == 503
    assert sleeps == []
    with execution.deadline(10, "tool demo_read"):
        assert request_with_retries(fake_http, "GET", URL, retries=3, sleep=sleeps.append).status == 200
    assert sleeps == [5.0]


def test_transient_errors_are_retried_but_definite_ones_are_not(fake_http: FakeHttp) -> None:
    def flaky(_request: RecordedRequest) -> Any:
        raise TransientHttpError("GET api.example.com: connection reset")

    fake_http.add_handler("GET", URL, flaky, repeat=False)
    fake_http.add("GET", URL, body="fine")
    sleeps: list[float] = []
    assert request_with_retries(fake_http, "GET", URL, retries=1, sleep=sleeps.append).body == b"fine"
    assert len(sleeps) == 1
    fake_http.add_error("GET", URL + "/other", "refusing an unsafe address")
    with pytest.raises(HttpError, match="refusing"):
        request_with_retries(fake_http, "GET", URL + "/other", retries=3, sleep=sleeps.append)
    assert len(sleeps) == 1


def test_exhausted_retries_return_the_last_response(fake_http: FakeHttp) -> None:
    fake_http.add("GET", URL, status=504, repeat=True)
    sleeps: list[float] = []
    assert request_with_retries(fake_http, "GET", URL, retries=2, sleep=sleeps.append).status == 504
    assert len(fake_http.requests) == 3
    assert len(sleeps) == 2


def test_exhausted_retries_raise_the_last_transient_error(fake_http: FakeHttp) -> None:
    def flaky(_request: RecordedRequest) -> Any:
        raise TransientHttpError("GET api.example.com: timed out")

    fake_http.add_handler("GET", URL, flaky)
    sleeps: list[float] = []
    with pytest.raises(TransientHttpError, match="timed out"):
        request_with_retries(fake_http, "GET", URL, retries=2, sleep=sleeps.append)
    assert len(fake_http.requests) == 3
    assert len(sleeps) == 2


def test_head_requests_may_be_retried(fake_http: FakeHttp) -> None:
    fake_http.add("HEAD", URL, status=503)
    fake_http.add("HEAD", URL)
    sleeps: list[float] = []
    assert request_with_retries(fake_http, "head", URL, retries=1, sleep=sleeps.append).status == 200
    assert [request.method for request in fake_http.requests] == ["HEAD", "HEAD"]


@pytest.mark.parametrize(
    ("method", "retries"), [("POST", 1), ("PUT", 2), ("DELETE", 3), ("GET", 4), ("GET", -1), ("HEAD", True)]
)
def test_retries_are_limited_to_a_few_idempotent_reads(
    fake_http: FakeHttp, method: str, retries: int
) -> None:
    with pytest.raises(ValueError, match="retr"):
        request_with_retries(fake_http, method, URL, retries=retries)
    with pytest.raises(ValueError, match="retr"):
        HttpClient().request(method, URL, retries=retries)
    assert fake_http.requests == []


def test_the_client_retries_through_its_own_single_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = urllib.error.HTTPError(URL, 503, "Service Unavailable", Message(), io.BytesIO(b""))
    opener = Opener([busy, lambda: Response([b"fine"])])
    client = client_with(monkeypatch, opener)
    monkeypatch.setattr(httpclient, "backoff_seconds", lambda _attempt: 0.0)

    response = client.request("GET", URL, retries=1)

    assert (response.status, response.body) == (200, b"fine")
    assert len(opener.timeouts) == 2
    with pytest.raises(ValueError, match="body"):
        client.request("GET", URL, body=b"x", retries=1)


def test_backoff_grows_and_stays_within_its_jitter_band() -> None:
    for attempt, (low, high) in enumerate([(0.25, 0.5), (0.5, 1.0), (1.0, 2.0)]):
        for _ in range(20):
            assert low <= backoff_seconds(attempt) <= high


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (None, None),
        ("7", 7.0),
        (" 0 ", 0.0),
        ("soon", None),
        ("-3", None),
        ("\u0661", None),
        ("9" * 13, None),
        ("Wed, 11 Nov 2026 12:00:30 GMT", 30.0),
        ("Wed, 11 Nov 2026 11:00:00 GMT", 0.0),
    ],
)
def test_retry_after_values(value: str | None, seconds: float | None) -> None:
    assert parse_retry_after(value, moment=datetime(2026, 11, 11, 12, 0, tzinfo=UTC)) == seconds
