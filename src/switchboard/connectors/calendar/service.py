"""The calendar connector's behaviour: fetching sources, caching events, and answering tool calls."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from switchboard import util
from switchboard.config import ensure_private_dir
from switchboard.connectors._dav import DavClient, DavError, throttled
from switchboard.connectors.calendar import caldav, ics, imip, rrule, slots
from switchboard.connectors.calendar.settings import NAME, Settings, Source
from switchboard.connectors.calendar.store import EventStore
from switchboard.context import AppContext
from switchboard.errors import HttpError, SecretError, SwitchboardError, ToolError
from switchboard.httpclient import request_with_retries

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 20_000_000
FEED_RETRIES = 2  # the feed fetch is a read-only GET, so transient failures may be repeated
CALDAV_PAST = timedelta(days=31)
CALDAV_FUTURE = timedelta(days=366)
MAX_AGENDA_SPAN = timedelta(days=366)
MAX_SLOT_SPAN = timedelta(days=62)
MAX_EVENT_LENGTH = timedelta(days=31)
MAX_SLOTS = 100
DESCRIPTION_LIMIT = 500
MAX_RESULT_WARNINGS = 20
GONE_STATUSES = frozenset({403, 404, 410})
DEFAULT_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR")

Sourced = tuple[str, rrule.Occurrence]


@dataclass(frozen=True)
class Fetched:
    """Raw calendars from one source; ``texts`` is None when the feed is unchanged (HTTP 304)."""

    texts: list[str] | None
    etag: str | None = None
    last_modified: str | None = None
    skipped: list[str] = field(default_factory=list)


def parse_moment(text: str, zone: tzinfo, *, end_of_day: bool = False) -> tuple[datetime, bool]:
    """Parse an ISO date or date-time from a tool argument; returns ``(aware datetime, is_date)``.

    Naive date-times use ``zone``. A date means midnight at its start, or with ``end_of_day`` the
    midnight that ends it, so ``end = "2026-11-12"`` includes all of that day.
    """
    value = text.strip()
    try:
        if len(value) == 10:
            day = date.fromisoformat(value) + timedelta(days=1 if end_of_day else 0)
            return datetime.combine(day, time(0), tzinfo=zone), True
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise ToolError(f"{util.truncate(value, 40)!r} is not an ISO date or date-time") from exc
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)), False


def webcal_to_https(url: str) -> str:
    """Calendar apps share feeds as ``webcal://`` links, which are ordinary HTTPS downloads."""
    scheme, separator, rest = url.strip().partition("://")
    return f"https://{rest}" if separator and scheme.lower() == "webcal" else url.strip()


def _age_seconds(stamp: Any, now: datetime) -> float:
    if not isinstance(stamp, str):
        return float("inf")
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def _describe(exc: Exception) -> str:
    """Error text safe to store and show: our own exceptions never carry secrets; others show only a type."""
    if isinstance(exc, SwitchboardError):
        return util.truncate(str(exc), 300)
    return f"unexpected {type(exc).__name__}; see the log"


def _retry_at(exc: Exception, now: datetime) -> str | None:
    """When a throttled source may be tried again, from the server's ``Retry-After``."""
    wait = exc.retry_after if isinstance(exc, DavError) else None
    return util.iso(now + timedelta(seconds=wait)) if wait else None


def _span(start: datetime, end: datetime) -> timedelta:
    return end.astimezone(UTC) - start.astimezone(UTC)


class CalendarService:
    """Everything the tools and the refresh job do. ``clock`` is injectable for tests."""

    def __init__(
        self, ctx: AppContext, settings: Settings, *, clock: Callable[[], datetime] = util.now
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.clock = clock
        self.store = EventStore(ctx.db, NAME)
        self._calendars: dict[str, list[caldav.CalendarCollection]] = {}

    # ---- sources -------------------------------------------------------------------------------

    def source_names(self, requested: Sequence[str] | None = None) -> list[str]:
        if not self.settings.sources:
            raise ToolError("no calendar sources are configured under [connectors.calendar.sources]")
        if not requested:
            return list(self.settings.sources)
        unknown = [name for name in requested if name not in self.settings.sources]
        if unknown:
            known = ", ".join(self.settings.sources)
            raise ToolError(f"unknown calendar source {', '.join(unknown)}; configured: {known}")
        return list(dict.fromkeys(requested))

    def secret(self, reference: str, source: Source) -> str:
        try:
            return self.ctx.secret(reference)
        except SecretError as exc:
            raise ToolError(f"calendar source {source.name!r}: {exc}") from exc

    def client(self, source: Source) -> DavClient:
        return DavClient(
            self.ctx.http,
            source.url,
            source.username,
            self.secret(source.password, source),
            f"calendar source {source.name!r}",
            allow_http=source.allow_http,
        )

    def calendars(
        self, source: Source, client: DavClient, *, refresh: bool = False
    ) -> list[caldav.CalendarCollection]:
        """Rediscover on source refreshes so newly added calendars become visible."""
        if refresh or source.name not in self._calendars:
            self._calendars[source.name] = caldav.find_calendars(client)
        return self._calendars[source.name]

    # ---- fetching ------------------------------------------------------------------------------

    def fetch(self, source: Source, state: Mapping[str, Any], now: datetime) -> Fetched:
        if source.type == "ics_file":
            return self._read_file(source)
        if source.type == "ics_url":
            return self._download(source, state)
        client = self.client(source)
        calendars = self.calendars(source, client, refresh=True)
        texts: list[str] = []
        skipped: list[str] = []
        first_gone: DavError | None = None
        for calendar in calendars:
            try:
                texts += caldav.query_events(client, calendar.url, now - CALDAV_PAST, now + CALDAV_FUTURE)
            except DavError as exc:
                if exc.status not in GONE_STATUSES:
                    raise  # throttling, credentials, and network failures affect every calendar alike
                first_gone = first_gone or exc
                skipped.append(f"skipped calendar {util.printable(calendar.name)!r}: {exc}")
        if first_gone is not None:
            self._calendars.pop(source.name, None)  # deleted, moved, or unshared: discover again next time
            if len(skipped) == len(calendars):
                raise first_gone
            log.warning("calendar source %s: %s", source.name, "; ".join(skipped))
        return Fetched(texts, skipped=skipped)

    def _read_file(self, source: Source) -> Fetched:
        path = Path(source.path).expanduser()
        try:
            if path.stat().st_size > MAX_FEED_BYTES:
                raise ToolError(
                    f"calendar source {source.name!r}: the file is larger than {MAX_FEED_BYTES} bytes"
                )
            return Fetched([path.read_text(encoding="utf-8", errors="replace")])
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ToolError(f"calendar source {source.name!r}: cannot read the file ({reason})") from exc

    def _download(self, source: Source, state: Mapping[str, Any]) -> Fetched:
        """GET the feed with a conditional request; the URL is a bearer secret and is never logged."""
        url = webcal_to_https(self.secret(source.url, source))
        headers = {"Accept": "text/calendar"}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])
        try:
            response = request_with_retries(
                self.ctx.http,
                "GET",
                url,
                retries=FEED_RETRIES,
                headers=headers,
                max_bytes=MAX_FEED_BYTES,
                allow_http=source.allow_http,
            )
        except HttpError as exc:
            raise ToolError(f"calendar source {source.name!r}: {exc}") from exc
        if response.status == 304:
            return Fetched(None, state.get("etag"), state.get("last_modified"))
        busy = throttled(f"calendar source {source.name!r}", response)
        if busy is not None:
            raise busy
        if not response.ok:
            raise ToolError(f"calendar source {source.name!r}: the feed answered HTTP {response.status}")
        return Fetched([response.text()], response.headers.get("etag"), response.headers.get("last-modified"))

    def _parse(self, texts: Sequence[str]) -> tuple[list[ics.Event], list[str]]:
        events: list[ics.Event] = []
        warnings: list[str] = []
        for text in texts:
            parsed = ics.parse_calendar(text, self.settings.zone)
            events += parsed.events
            warnings += [warning for warning in parsed.warnings if warning not in warnings]
        return events, warnings[: ics.MAX_WARNINGS]

    def refresh(self, name: str, now: datetime) -> dict[str, Any]:
        """Fetch one source and replace its cached events. Failures are recorded in its state, not raised."""
        source = self.settings.sources[name]
        state = self.store.state(name)
        if _age_seconds(state.get("retry_at"), now) < 0:
            return state  # the server asked for a pause with Retry-After
        state["checked_at"] = util.iso(now)
        try:
            fetched = self.fetch(source, state, now)
            if fetched.texts is not None:
                events, warnings = self._parse(fetched.texts)
                count = self.store.replace(name, events)
                state.update(
                    etag=fetched.etag, last_modified=fetched.last_modified, events=count, warnings=warnings
                )
            state.update(fetched_at=util.iso(now), error=None, retry_at=None, skipped=fetched.skipped)
        except Exception as exc:  # one broken source must never stop the others
            if not isinstance(exc, SwitchboardError):
                log.exception("calendar source %s failed to refresh", name)
            state["error"] = _describe(exc)
            state["retry_at"] = _retry_at(exc, now)
        self.store.save_state(name, state)
        return state

    def ensure_fresh(self, names: Sequence[str], now: datetime) -> list[str]:
        """Refresh sources not checked within the refresh interval; returns warnings for failing ones."""
        warnings: list[str] = []
        for name in names:
            state = self.store.state(name)
            if _age_seconds(state.get("checked_at"), now) >= self.settings.refresh_seconds:
                state = self.refresh(name, now)
            if state.get("error"):
                cached = " (showing cached events)" if state.get("fetched_at") else ""
                warnings.append(f"{name}: {state['error']}{cached}")
            warnings += [f"{name}: {note}" for note in state.get("skipped") or []]
        return warnings

    def occurrences(
        self, names: Sequence[str], start: datetime, end: datetime
    ) -> tuple[list[Sourced], list[str]]:
        found: list[Sourced] = []
        warnings: list[str] = []
        for name, events in self.store.load(names, start, end).items():
            expanded, notes = rrule.expand(events, start, end)
            found += [(name, item) for item in expanded]
            warnings += [f"{name}: {note}" for note in notes]
        found.sort(key=lambda pair: (pair[1].start.astimezone(UTC), pair[1].event.summary, pair[0]))
        return found, warnings

    def emit_upcoming(self, now: datetime) -> int:
        """Emit ``calendar.upcoming`` for timed instances starting within ``upcoming_minutes``."""
        horizon = now + timedelta(minutes=self.settings.upcoming_minutes)
        found, _ = self.occurrences(list(self.settings.sources), now, horizon)
        emitted = 0
        for source, item in found:
            event = item.event
            if (
                event.all_day
                or event.status == "CANCELLED"
                or item.start.astimezone(UTC) < now.astimezone(UTC)
            ):
                continue
            payload = {
                "calendar": source,
                "uid": event.uid,
                "summary": util.truncate(event.summary, 200),
                "start": self.when(item.start),
                "end": self.when(item.end),
                "location": util.truncate(event.location, 200),
                "minutes_until": int(_span(now, item.start).total_seconds() // 60),
            }
            key = f"cal:{source}:{event.uid}:{util.iso(item.start)}"
            if self.ctx.events.emit(NAME, "calendar.upcoming", payload, dedupe_key=key) is not None:
                emitted += 1
        return emitted

    def refresh_all(self, now: datetime) -> None:
        for name in self.settings.sources:
            self.refresh(name, now)
        self.emit_upcoming(now)

    def run_job(self) -> None:
        self.refresh_all(self.clock())

    # ---- rendering -----------------------------------------------------------------------------

    def when(self, moment: datetime, all_day: bool = False) -> str:
        """ISO text in the configured zone; all-day values are plain dates (end dates are exclusive)."""
        local = moment.astimezone(self.settings.zone)
        return local.date().isoformat() if all_day else local.isoformat(timespec="seconds")

    def render(self, source: str, item: rrule.Occurrence) -> dict[str, Any]:
        event = item.event
        data: dict[str, Any] = {
            "source": source,
            "uid": event.uid,
            "summary": util.truncate(event.summary, 200),
            "start": self.when(item.start, event.all_day),
            "end": self.when(item.end, event.all_day),
            "all_day": event.all_day,
            "recurring": item.recurring,
        }
        if event.location:
            data["location"] = util.truncate(event.location, 200)
        if event.description:
            data["description"] = util.truncate(event.description, DESCRIPTION_LIMIT)
        if event.status:
            data["status"] = event.status
        if event.organizer:
            data["organizer"] = event.organizer
        if event.attendees:
            data["attendees"] = list(event.attendees[:10])
            data["attendee_count"] = len(event.attendees)
        return data

    def window(self, args: Mapping[str, Any], longest: timedelta) -> tuple[datetime, datetime]:
        start, _ = parse_moment(str(args["start"]), self.settings.zone)
        end, _ = parse_moment(str(args["end"]), self.settings.zone, end_of_day=True)
        if _span(start, end) <= timedelta(0):
            raise ToolError("end must be after start")
        if _span(start, end) > longest:
            raise ToolError(f"the window may span at most {longest.days} days")
        return start, end

    # ---- read tools ----------------------------------------------------------------------------

    def tool_sources(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        listed = []
        for source in self.settings.sources.values():
            state = self.store.state(source.name)
            listed.append(
                {
                    "name": source.name,
                    "type": source.type,
                    "writable": source.type == "caldav" and source.writable,
                    "fetched_at": state.get("fetched_at"),
                    "events": state.get("events"),
                    "error": state.get("error"),
                    "warnings": len(state.get("warnings") or []),
                }
            )
        return {"time_zone": ics.zone_key(self.settings.zone), "sources": listed}

    def tool_agenda(self, args: Mapping[str, Any]) -> dict[str, Any]:
        start, end = self.window(args, MAX_AGENDA_SPAN)
        names = self.source_names(args.get("sources"))
        warnings = self.ensure_fresh(names, self.clock())
        found, notes = self.occurrences(names, start, end)
        limit = int(args.get("limit", 50))
        return {
            "untrusted_content": True,
            "time_zone": ics.zone_key(self.settings.zone),
            "start": self.when(start),
            "end": self.when(end),
            "events": [self.render(source, item) for source, item in found[:limit]],
            "total": len(found),
            "truncated": len(found) > limit,
            "warnings": (warnings + notes)[:MAX_RESULT_WARNINGS],
        }

    def tool_free_slots(self, args: Mapping[str, Any]) -> dict[str, Any]:
        start, end = self.window(args, MAX_SLOT_SPAN)
        try:
            day_start, day_end = slots.parse_working_hours(str(args.get("working_hours", "09:00-17:00")))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        weekdays = {slots.WEEKDAYS.index(code) for code in args.get("weekdays", DEFAULT_WEEKDAYS)}
        names = self.source_names(args.get("sources"))
        warnings = self.ensure_fresh(names, self.clock())
        found, notes = self.occurrences(names, start, end)
        busy = [
            (item.start, item.end)
            for _, item in found
            if item.event.status != "CANCELLED" and not item.event.transparent
        ]
        free = slots.free_slots(
            busy,
            start,
            end,
            zone=self.settings.zone,
            day_start=day_start,
            day_end=day_end,
            weekdays=weekdays,
            minimum=timedelta(minutes=int(args["duration_minutes"])),
            limit=MAX_SLOTS,
        )
        return {
            "time_zone": ics.zone_key(self.settings.zone),
            "slots": [
                {
                    "start": self.when(low),
                    "end": self.when(high),
                    "minutes": int(_span(low, high).total_seconds() // 60),
                }
                for low, high in free
            ],
            "warnings": (warnings + notes)[:MAX_RESULT_WARNINGS],
        }

    # ---- write tools ---------------------------------------------------------------------------

    def owner(self) -> str:
        if not self.settings.owner_email:
            raise ToolError("set owner_email in [connectors.calendar] to send or draft invitations")
        return self.settings.owner_email

    def new_event(
        self, args: Mapping[str, Any], *, organizer: str = "", attendees: Sequence[str] = ()
    ) -> ics.Event:
        """An event from tool arguments; a date-only start makes an all-day event (end date inclusive)."""
        zone = self.settings.zone
        summary = str(args["summary"]).strip()
        if not summary:
            raise ToolError("summary must not be blank")
        start, all_day = parse_moment(str(args["start"]), zone)
        end = self._event_end(args, start, all_day)
        if _span(start, end) <= timedelta(0):
            raise ToolError("end must be after start")
        if _span(start, end) > MAX_EVENT_LENGTH:
            raise ToolError(f"events may last at most {MAX_EVENT_LENGTH.days} days")
        return ics.Event(
            uid=str(uuid.uuid4()),
            summary=summary,
            start=start,
            end=end,
            all_day=all_day,
            description=str(args.get("description", "")).strip(),
            location=str(args.get("location", "")).strip(),
            organizer=organizer,
            attendees=tuple(attendees),
        )

    def _event_end(self, args: Mapping[str, Any], start: datetime, all_day: bool) -> datetime:
        zone = self.settings.zone
        if all_day:
            if "duration_minutes" in args:
                raise ToolError("an all-day event takes an end date, not duration_minutes")
            if "end" not in args:
                return start + timedelta(days=1)
            end, end_is_date = parse_moment(str(args["end"]), zone, end_of_day=True)
            if not end_is_date:
                raise ToolError("when start is a date, end must be a date too")
            return end
        if "end" in args:
            end, end_is_date = parse_moment(str(args["end"]), zone)
            if end_is_date:
                raise ToolError("when start has a time, end must have a time too")
            return end
        if "duration_minutes" in args:
            return (start.astimezone(UTC) + timedelta(minutes=int(args["duration_minutes"]))).astimezone(
                start.tzinfo
            )
        raise ToolError("give either end or duration_minutes")

    def _confirmation(self, event: ics.Event) -> dict[str, Any]:
        return {
            "uid": event.uid,
            "summary": event.summary,
            "start": self.when(event.start, event.all_day),
            "end": self.when(event.end, event.all_day),
            "all_day": event.all_day,
            "attendees": list(event.attendees),
        }

    def write_outbox(self, event: ics.Event, stamp: datetime) -> Path:
        """Save the invitation as ``<data_dir>/calendar/outbox/<uid>.ics`` with mode 600."""
        folder = ensure_private_dir(ensure_private_dir(self.ctx.data_dir / NAME) / "outbox")
        path = folder / f"{event.uid}.ics"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="") as handle:
            handle.write(ics.serialize(event, stamp=stamp, method="REQUEST"))
        return path

    def tool_propose(self, args: Mapping[str, Any]) -> dict[str, Any]:
        owner = self.owner()
        attendees = list(args.get("attendees") or [owner])
        event = self.new_event(args, organizer=owner, attendees=attendees)
        stamp = self.clock()
        result = self._confirmation(event)
        drafts = self.ctx.service("mail.drafts")
        if drafts is None:
            path = self.write_outbox(event, stamp)
            return {
                **result,
                "status": "file_saved",
                "path": str(path),
                "next_step": "Attach this .ics to a message to yourself, open it in your calendar app, or "
                "import it in Google Calendar (Settings > Import & export).",
            }
        message = imip.build_request(
            event, organizer=owner, recipients=[owner], stamp=stamp, zone=self.settings.zone
        )
        reference = drafts.save_draft(message, self.settings.invite_account)
        return {
            **result,
            "status": "draft_saved",
            "draft": str(reference),
            "next_step": "Open the draft in your mail app and send it to yourself or to the attendees; "
            "calendar apps add the event from the invitation.",
        }

    def writable_source(self, name: str) -> Source:
        source = self.settings.sources.get(name)
        if source is None:
            raise ToolError(f"unknown calendar source {name!r}")
        if source.type != "caldav" or not source.writable:
            raise ToolError(
                f"calendar source {name!r} is read-only; "
                "only caldav sources with writable = true accept events"
            )
        return source

    def tool_create(self, args: Mapping[str, Any]) -> dict[str, Any]:
        source = self.writable_source(str(args["source"]))
        attendees = list(args.get("attendees") or [])
        event = self.new_event(args, organizer=self.owner() if attendees else "", attendees=attendees)
        client = self.client(source)
        calendar = self.calendars(source, client)[0]
        try:
            caldav.put_event(client, calendar.url, event.uid, ics.serialize(event, stamp=self.clock()))
        except DavError as exc:
            if exc.status in GONE_STATUSES:
                self._calendars.pop(source.name, None)  # the next attempt discovers the calendars again
            raise
        state = self.store.state(source.name)
        state["checked_at"] = None  # the next read refreshes this source
        self.store.save_state(source.name, state)
        return {
            **self._confirmation(event),
            "status": "created",
            "source": source.name,
            "calendar": calendar.name,
        }

    def tool_send_invite(self, args: Mapping[str, Any]) -> dict[str, Any]:
        owner = self.owner()
        sender = self.ctx.service("mail.sender")
        if sender is None:
            raise ToolError("sending invitations needs the mail.sender service; enable the mail connector")
        attendees = list(args["attendees"])
        event = self.new_event(args, organizer=owner, attendees=attendees)
        message = imip.build_request(
            event, organizer=owner, recipients=attendees, stamp=self.clock(), zone=self.settings.zone
        )
        message_id = sender.send(message, self.settings.invite_account)
        return {**self._confirmation(event), "status": "sent", "message_id": str(message_id)}
