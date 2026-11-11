"""Shared fakes for tests. Nothing here touches the network."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from switchboard.errors import HttpError
from switchboard.httpclient import HttpResponse, check_url


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float | None = None
    max_bytes: int | None = None
    follow_redirects: bool = True
    public_only: bool = False


Responder = Callable[[RecordedRequest], HttpResponse]


@dataclass
class _Route:
    method: str
    url: str
    respond: Responder
    repeat: bool


class FakeHttp:
    """Stands in for :class:`switchboard.httpclient.HttpClient`.

    Register exact ``(method, url)`` routes; unexpected requests fail the test loudly. The HTTPS rule
    is still enforced so connectors cannot accidentally rely on plain HTTP.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._routes: list[_Route] = []

    def add(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        body: bytes | str = b"",
        headers: Mapping[str, str] | None = None,
        repeat: bool = False,
    ) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        lowered = {key.lower(): value for key, value in (headers or {}).items()}

        def respond(request: RecordedRequest) -> HttpResponse:
            return HttpResponse(status=status, url=request.url, headers=lowered, body=payload)

        self._routes.append(_Route(method.upper(), url, respond, repeat))

    def add_error(
        self, method: str, url: str, message: str = "connection refused", *, repeat: bool = False
    ) -> None:
        """Simulate a network failure (DNS, TLS, timeout) for one route."""

        def fail(_request: RecordedRequest) -> HttpResponse:
            raise HttpError(message)

        self._routes.append(_Route(method.upper(), url, fail, repeat))

    def add_handler(self, method: str, url: str, respond: Responder, *, repeat: bool = True) -> None:
        self._routes.append(_Route(method.upper(), url, respond, repeat))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        allow_http: bool = False,
        follow_redirects: bool = True,
        public_only: bool = False,
    ) -> HttpResponse:
        check_url(url, allow_http=allow_http)
        recorded = RecordedRequest(
            method.upper(),
            url,
            {k.lower(): v for k, v in (headers or {}).items()},
            body,
            timeout=timeout,
            max_bytes=max_bytes,
            follow_redirects=follow_redirects,
            public_only=public_only,
        )
        self.requests.append(recorded)
        for index, route in enumerate(self._routes):
            if route.method == recorded.method and route.url == url:
                if not route.repeat:
                    del self._routes[index]
                return route.respond(recorded)
        raise AssertionError(f"unexpected HTTP request: {recorded.method} {url}")

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)


@dataclass
class FakeMailService:
    """Records drafts and sends; provide it as both ``mail.drafts`` and ``mail.sender``."""

    drafts: list[tuple[str | None, EmailMessage]] = field(default_factory=list)
    sent: list[tuple[str | None, EmailMessage]] = field(default_factory=list)

    def save_draft(self, message: EmailMessage, account: str | None = None) -> str:
        self.drafts.append((account, message))
        return f"draft-{len(self.drafts)}"

    def send(self, message: EmailMessage, account: str | None = None) -> str:
        self.sent.append((account, message))
        return str(message.get("Message-ID", f"<fake-{len(self.sent)}@test.invalid>"))


@dataclass
class FakeNotifier:
    """Records notifications; provide it as ``notify``."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    def send(
        self,
        title: str,
        message: str,
        *,
        priority: str = "default",
        url: str | None = None,
    ) -> list[str]:
        self.messages.append({"title": title, "message": message, "priority": priority, "url": url})
        return ["fake"]
