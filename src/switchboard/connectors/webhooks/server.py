"""The HTTP side of the webhook receiver, built on :class:`http.server.ThreadingHTTPServer`.

Only ``POST /hooks/<name>`` is served. Size is checked from ``Content-Length`` before any body byte
is read; rejected bodies are drained (up to a bound) after the reply so the client sees the status
instead of a reset connection. Responses are short plain text, and the access log never records
query strings.

Slow clients are bounded twice: at most ``MAX_CONNECTIONS`` connections are served at once (extra ones
are closed immediately), and every connection has a total deadline, not just a per-read timeout, so a
client trickling one byte at a time cannot hold a thread.
"""

from __future__ import annotations

import contextlib
import logging
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from switchboard.connectors.webhooks.receiver import WebhookReceiver
from switchboard.connectors.webhooks.verify import Reply, Request

log = logging.getLogger(__name__)

SOCKET_TIMEOUT_SECONDS = 10.0
CONNECTION_DEADLINE_SECONDS = 30.0
MAX_CONNECTIONS = 32
DRAIN_LIMIT_BYTES = 1024 * 1024
_CONTENT_LENGTH = re.compile(r"[0-9]{1,15}")


class WebhookServer(ThreadingHTTPServer):
    """A threaded HTTP server that hands every POST to one :class:`WebhookReceiver`."""

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], receiver: WebhookReceiver, *, max_connections: int = MAX_CONNECTIONS
    ) -> None:
        self.receiver = receiver
        self._slots = threading.BoundedSemaphore(max_connections)
        self._holders: set[Any] = set()
        self._holders_lock = threading.Lock()
        super().__init__(address, WebhookHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            log.warning("webhook receiver is busy; closing an extra connection")
            super().shutdown_request(request)  # the base close; this connection holds no slot
            return
        with self._holders_lock:
            self._holders.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        """Close the connection and free its slot exactly once, whichever path closes it."""
        try:
            super().shutdown_request(request)
        finally:
            with self._holders_lock:
                held = request in self._holders
                self._holders.discard(request)
            if held:
                self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # One line instead of socketserver's traceback; clients that hang up early are routine.
        error = sys.exc_info()[1]
        log.warning("webhook connection ended early (%s)", type(error).__name__ if error else "unknown")


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "switchboard-webhooks"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS  # per read; CONNECTION_DEADLINE_SECONDS bounds the whole connection
    connection_deadline = CONNECTION_DEADLINE_SECONDS
    error_content_type = "text/plain; charset=utf-8"
    error_message_format = "%(code)d %(explain)s\n"

    def setup(self) -> None:
        super().setup()
        self._deadline = threading.Timer(self.connection_deadline, self._expire)
        self._deadline.daemon = True
        self._deadline.start()

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _expire(self) -> None:
        """Cut a connection that outlived its deadline; blocked reads then end immediately."""
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def do_POST(self) -> None:
        reply, unread = self._process_post()
        self._finish(reply, unread)

    def do_GET(self) -> None:
        self._not_allowed()

    def do_HEAD(self) -> None:
        self._not_allowed()

    def do_PUT(self) -> None:
        self._not_allowed()

    def do_PATCH(self) -> None:
        self._not_allowed()

    def do_DELETE(self) -> None:
        self._not_allowed()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Never the query string. ``path`` and ``command`` are unset when the request line was malformed.
        path = _split_target(getattr(self, "path", "") or "")
        command = getattr(self, "command", None) or "-"
        log.info("%s %s -> %s", command, (path.path if path else "")[:200], code)

    def log_message(self, format: str, *args: Any) -> None:
        # http.server's own messages can quote the raw request line, query string included.
        log.debug("http.server note: %s", format)

    def _process_post(self) -> tuple[Reply, int]:
        """Decide the reply; also return how many unread body bytes to drain afterwards."""
        receiver = self._receiver()
        length, problem = self._declared_length()
        split = _split_target(self.path)
        hook = receiver.hook_for_path(split.path) if split else None
        if split is None or hook is None:
            return Reply(404, "unknown hook\n"), length
        if problem is not None:
            return problem, 0
        if length > receiver.settings.max_body_bytes:
            return Reply(413, "body too large\n"), length
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            return Reply(400, "incomplete body\n"), 0
        request = Request(headers=self._header_map(), query=urllib.parse.parse_qs(split.query), body=body)
        try:
            return receiver.handle(hook, request, now=time.time()), 0
        except Exception as exc:
            log.error("webhook %s failed (%s)", hook.name, type(exc).__name__)
            return Reply(500, "internal error\n"), 0

    def _not_allowed(self) -> None:
        length, problem = self._declared_length()
        self._finish(Reply(405, "only POST is accepted\n"), 0 if problem else length)

    def _declared_length(self) -> tuple[int, Reply | None]:
        if self.headers.get("Transfer-Encoding") is not None:
            return 0, Reply(411, "send a Content-Length; chunked bodies are not accepted\n")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0, None
        text = str(raw).strip()
        if not _CONTENT_LENGTH.fullmatch(text):
            return 0, Reply(400, "invalid Content-Length\n")
        return int(text), None

    def _header_map(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            headers.setdefault(name.lower(), str(value))
        return headers

    def _finish(self, reply: Reply, unread: int) -> None:
        body = reply.text.encode("utf-8")
        self.send_response(reply.status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if reply.status == 405:
            self.send_header("Allow", "POST")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._drain(unread)
        self.close_connection = True

    def _drain(self, unread: int) -> None:
        """Read and drop a refused body so closing the socket does not reset the connection."""
        remaining = unread if unread <= DRAIN_LIMIT_BYTES else 0
        with contextlib.suppress(OSError):
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

    def _receiver(self) -> WebhookReceiver:
        server = self.server
        if not isinstance(server, WebhookServer):
            raise TypeError("WebhookHandler only runs inside a WebhookServer")
        return server.receiver


def _split_target(target: str) -> urllib.parse.SplitResult | None:
    """Split a request target; None when it cannot be parsed (for example a broken IPv6 host)."""
    try:
        return urllib.parse.urlsplit(target)
    except ValueError:
        return None
