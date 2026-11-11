# Calendar connector

The calendar connector lets assistants read your calendars, find free time, and prepare or send
invitations, all through the policy gate. It reads three kinds of sources:

* **`ics_url`**: a private iCalendar (`.ics`) feed address, such as Google Calendar's "secret
  address in iCal format". Read-only.
* **`caldav`**: a calendar server that speaks CalDAV (Calendaring Extensions to Web Distributed
  Authoring and Versioning, or WebDAV), defined in Request for Comments (RFC) 4791, such as iCloud,
  Fastmail, Nextcloud, or Radicale. Read-only unless you set `writable = true`.
* **`ics_file`**: an `.ics` file on this machine. Read-only.

No paid aggregator and no automation service sits in between. Feeds are downloaded directly from
your provider and parsed with the Python standard library.

## Tools

| Name                     | Tier  | Purpose                                                                                   |
| ------------------------ | ----- | ----------------------------------------------------------------------------------------- |
| `calendar_sources`       | read  | List sources with their type, last refresh, event count, and last error (no addresses)    |
| `calendar_agenda`        | read  | Events between `start` and `end`, recurring events expanded, in your configured time zone |
| `calendar_free_slots`    | read  | Free windows of at least `duration_minutes` inside working hours on chosen weekdays       |
| `calendar_propose_event` | draft | Save an invitation as a mail draft to you (or an `.ics` file); nothing is sent            |
| `calendar_create_event`  | act   | Create a new event on a writable CalDAV calendar after you approve                        |
| `calendar_send_invite`   | act   | Email an invitation to attendees after you approve                                        |

Argument notes:

* `start` and `end` take a date (`2026-11-12`) or a date-time (`2026-11-12T09:30`,
  `2026-11-12T14:30:00Z`). Times without an offset use the configured `timezone`. A date-only `end`
  includes that whole day, so `start = end = "2026-11-12"` means one day.
* `calendar_agenda` spans at most 366 days and returns at most `limit` events (default 50, maximum
  200), with `total` and `truncated` so the assistant knows when it saw only part of the list.
  Descriptions are cut to 500 characters. Results carry `"untrusted_content": true` because titles,
  places, and descriptions come from other people.
* `calendar_free_slots` spans at most 62 days and returns at most 100 windows. `working_hours`
  defaults to `09:00-17:00` (an end of `24:00` means midnight) and `weekdays` to `MO`–`FR`.
  Cancelled events and events marked free (`TRANSP:TRANSPARENT`) do not block time.
* The event tools take `summary`, `start`, and either `end` or `duration_minutes`, plus optional
  `location`, `description`, and `attendees`. A date-only `start` makes an all-day event; its `end`
  date is the last day of the event. `calendar_create_event` also needs `source`, and
  `calendar_send_invite` needs at least one attendee.
* Approval summaries read like `create 'Design review' on 2026-11-20 10:00 America/New_York in work`
  or `email an invitation for 'Lunch' on … to sam@example.com`. Both act tools declare their
  attendees as recipients, so `allow_recipients` in the policy applies to them.

## Events, jobs, and services

* **Job `calendar.refresh`** runs every `refresh_interval_seconds` (default 900). It refreshes every
  source, replaces that source's cached events in one transaction, and then emits
  `calendar.upcoming`. One failing source never stops the others; its error is kept and shown by
  `calendar_sources`, `calendar_agenda`, and `switchboard doctor`.
* **Event `calendar.upcoming`**, with payload keys `calendar` (the source name), `uid`, `summary`,
  `start`, `end`, `location`, and `minutes_until`, for each timed instance starting within
  `upcoming_minutes` (default 30). The dedupe key `cal:<source>:<uid>:<start in UTC>`, with the
  start in Coordinated Universal Time (UTC), makes it fire once per instance. All-day and
  cancelled events are not announced.
* **Subscribes** to no events and **provides** no services.
* **Uses** the mail connector's `mail.drafts` service (`calendar_propose_event`) and `mail.sender`
  service (`calendar_send_invite`), looked up when the tool runs. Without the mail connector,
  `calendar_propose_event` writes the invitation to `<data_dir>/calendar/outbox/<uid>.ics` (mode
  600) instead, and `calendar_send_invite` fails with a clear message.
* Reading tools refresh a source on demand when it was not checked within the refresh interval, so
  the first agenda call after startup does not wait for the runner.

## Setup

Add `"calendar"` to `[switchboard] connectors`, copy the parts you need from
[docs/config/calendar.toml](../config/calendar.toml), and run `switchboard doctor`. It checks every
secret reference and file path without printing values, and reports the last refresh error of each
source. Set `owner_email` if you want the invitation tools.

### Google Calendar (checked 2026-11-11)

* **Secret address (read-only).** In Google Calendar on a computer: Settings → Settings, choose the
  calendar under "Settings for my calendars", open **Integrate calendar**, and copy the **Secret
  address in iCal format**. Google says only you should know it; if it leaks, **Reset** invalidates
  the old address. Work or school accounts may hide it if an administrator restricted sharing.
  Source: [Google Calendar Help, "Sync your calendar with computer programs"](https://support.google.com/calendar/answer/37648).
  Save the address in a file with mode 600 and use `url = "file:~/…"`; `webcal://` links work too.
* **Google's CalDAV and CardDAV endpoints need OAuth 2.0**, the authorization framework of
  RFC 6749. (CardDAV, the address book counterpart of CalDAV, is defined in RFC 6352.) Google's CalDAV
  guide requires Hypertext Transfer Protocol Secure (HTTPS) with OAuth 2.0 and says: "Attempting to
  connect over HTTP or using Basic Authentication results in an HTTP `401 Unauthorized` status code"
  ([Google CalDAV guide](https://developers.google.com/workspace/calendar/caldav/v2/guide)). Google's
  CardDAV page says Google "does not support any other authentication method"
  ([Google CardDAV](https://developers.google.com/people/carddav)). Both also need a client
  registered in a Google Cloud project. Switchboard does not implement OAuth, so Google calendars are
  read through the secret address only.
* **Adding events to Google without OAuth.** `calendar_propose_event` saves a standard invitation
  (an iCalendar Message-Based Interoperability Protocol, or iMIP, message; RFC 6047) as a draft in
  your own mailbox through the mail connector. You open the draft and send it to yourself or to the
  attendees. When no mail connector is enabled, the `.ics` file it writes can be imported in Google
  Calendar under Settings → **Import & export**; Google notes that guests and conference data are not
  imported that way ([Google Calendar Help, "Import events"](https://support.google.com/calendar/answer/37118)).
  How Gmail treats an invitation whose organizer is also the recipient is not documented on those
  pages; check it once with a test event.

### iCloud (checked 2026-11-11)

* Use an **app-specific password**. Apple requires two-factor authentication for them; create one at
  account.apple.com under **Sign-In and Security → App-Specific Passwords**. You can have up to 25,
  and changing your main Apple Account password revokes them all
  ([Apple Support 102654](https://support.apple.com/en-us/102654)).
* Apple's page does not list server names. The vdirsyncer project, which is regularly tested
  against iCloud, uses `https://caldav.icloud.com/` and `https://contacts.icloud.com/`
  ([vdirsyncer iCloud notes](https://vdirsyncer.pimutils.org/en/stable/tutorials/icloud.html)).
  Discovery then moves to per-account hosts such as `pNN-caldav.icloud.com`, which Switchboard
  accepts because they share the parent domain of the configured host.

### Fastmail (checked 2026-11-11)

* CalDAV server `https://caldav.fastmail.com/` (CardDAV is `https://carddav.fastmail.com/`), with
  an **app password**; your regular password does not work, and Basic plans do not include CalDAV or
  CardDAV ([Fastmail, "Server names and ports"](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports)).
* For a single calendar, Fastmail shows its **CalDAV URL** (the calendar's web address) under
  Settings → Calendars → Export.

### Self-hosted servers

Point `url` at the calendar collection or at the server root and let discovery find the calendars.
Plain `http://` is accepted for `localhost` and, with `allow_http = true`, for a server on your own
network.

## How it works

* **Parsing** follows a subset of RFC 5545 (iCalendar): folded lines, quoted parameters, text
  escapes, `TZID` time zones looked up by Internet Assigned Numbers Authority (IANA) name with
  `zoneinfo`, floating times and all-day dates in your configured zone, and `DTEND`, `DURATION`, or the
  RFC defaults for the end.
* **Recurrence** expands `RRULE` with `FREQ` daily, weekly, monthly, or yearly; `INTERVAL`, `COUNT`,
  `UNTIL`, `BYDAY` (including ordinals such as `2MO` and `-1FR`), `BYMONTHDAY` (including negative
  days), `BYMONTH`, and `WKST`. `EXDATE` removes instances and `RECURRENCE-ID` overrides replace them.
  Expansion runs on the event's own wall clock, so a 09:00 meeting stays at 09:00 across daylight
  saving changes.
* **Discovery** follows RFC 6764: the configured Uniform Resource Locator (URL) may be a calendar,
  a principal, or a server root; otherwise `/.well-known/caldav` leads to `current-user-principal`, then
  `calendar-home-set`, then the calendars that accept events. Discovered calendars are remembered
  until one answers 403, 404 or 410 (deleted, moved, or no longer shared). That calendar is skipped
  with a warning in tool results, the others still refresh, and the next refresh runs discovery again.
* **CalDAV reads** use one `calendar-query` REPORT per calendar with a `VEVENT` filter and a
  UTC `time-range` from 31 days ago to 366 days ahead; agenda requests outside that range see only
  what the cache holds. **Feeds** are downloaded with `If-None-Match` and `If-Modified-Since`, so an
  unchanged feed costs a `304 Not Modified`. A `429` or `503` reply whose `Retry-After` gives a
  delay in seconds pauses that source until then (at most a day).
* **Writes** are a single `PUT` of `<calendar>/<uid>.ics` with `If-None-Match: *`, so an existing
  event is never overwritten. New events are written in UTC (or as dates), so no `VTIMEZONE` is
  needed; their unique identifier (UID) is a random universally unique identifier (UUID), as
  RFC 7986 recommends.
* **Invitations** follow RFC 5546 and RFC 6047: a `multipart/alternative` message with a
  `text/plain` part and a `text/calendar; method=REQUEST` part holding `METHOD:REQUEST`, `ORGANIZER`,
  one `ATTENDEE;RSVP=TRUE` per invitee, `UID`, `DTSTAMP`, and `SEQUENCE:0`. Where a draft lands is up
  to the mail connector; Internet Message Access Protocol (IMAP) servers mark the drafts mailbox
  with the `\Drafts` special-use attribute of RFC 6154.
* **Cache**: parsed events live in the `calendar_events` table; feed validators, refresh times, and
  errors live in the core key-value table.

## Tradeoffs and limits

* **A secret feed address is a bearer secret.** Anyone holding it can read that calendar, with no
  password and no log you can see. Keep it in a secret reference, never in the configuration file,
  and reset it in Google Calendar if it leaks. Switchboard never logs, returns, or includes it in an
  error message.
* **Read-only feeds versus CalDAV.** A feed is simple and works with Google without OAuth, but it is
  read-only and only as fresh as the provider makes it; Google does not document how often its
  secret address updates. CalDAV is current and can be written to, but needs an app-specific
  password and a provider that allows Basic authentication.
* **The invitation path is manual on purpose.** For Google, `calendar_propose_event` gives you a
  draft to send; nothing reaches your calendar until you act.
* **Recurrence subset.** `BYSETPOS`, `BYWEEKNO`, `BYYEARDAY`, `BYHOUR`, `BYMINUTE`, `BYSECOND`, and
  hourly or finer frequencies are not expanded: the agenda shows only the first instance and a
  warning. `RDATE` is ignored with a warning. Each event expands to at most 1,000 instances per
  query.
* **Time zones.** Embedded `VTIMEZONE` definitions are ignored. Names that `zoneinfo` does not know,
  such as Windows names from Outlook, fall back to your configured zone with a warning, so those
  times can be off by the difference.
* **Polling, not push.** Sources are polled every `refresh_interval_seconds`, and
  `calendar.upcoming` is checked at the same moments, so keep `upcoming_minutes` at least as long as
  the interval (`switchboard doctor` warns otherwise). WebDAV sync tokens (RFC 6578) are not used.
* **Server-side scheduling.** When `calendar_create_event` includes attendees, a server that
  implements CalDAV scheduling (RFC 6638) may email them on its own. The approval summary says so.
* **Free time** considers only the configured calendars. All-day events block whole days unless
  they are marked free.

## Security notes

* Credentials and feed addresses are secret references resolved only when a tool or job runs.
  Error messages name the source, never the address or credentials.
* Credentials go only to the configured origin or, over HTTPS, to a host under the same parent
  domain. Redirects anywhere else are refused, as are downgrades from HTTPS to plain Hypertext
  Transfer Protocol (HTTP).
* Server replies are parsed with `xml.etree.ElementTree`, which does not fetch external entities.
  An expat pre-scan refuses any reply that declares a `DOCTYPE` or entities, even one placed after a
  comment, before a single entity is expanded.
* Calendar text is untrusted. Agenda results are flagged `untrusted_content`, and descriptions are
  capped. The connector never follows links found in events.
* `ics_file` paths come only from your configuration; assistants cannot choose files. Outbox files
  are created with mode 600 inside the private data directory.
* Only the act-tier tools change anything outside Switchboard, and the policy asks you first by
  default. The connector never edits or deletes existing events.

## Configuration reference

| Key                                 | Default | Meaning                                                              |
| ----------------------------------- | ------- | -------------------------------------------------------------------- |
| `timezone`                          | `UTC`   | IANA zone for display, floating times, and all-day dates             |
| `refresh_interval_seconds`          | `900`   | Refresh period for every source (60 to 604,800)                      |
| `upcoming_minutes`                  | `30`    | How far ahead `calendar.upcoming` looks (1 to 1,440)                 |
| `owner_email`                       | none    | Organizer of invitations; drafts are addressed to it                 |
| `invite_account`                    | none    | Mail connector account for drafts and invitations                    |
| `sources.<name>.type`               | —       | `ics_url`, `caldav`, or `ics_file`                                   |
| `sources.<name>.url`                | —       | `ics_url`: secret reference; `caldav`: calendar or server address    |
| `sources.<name>.username`           | —       | `caldav` user name                                                   |
| `sources.<name>.password`           | —       | `caldav` secret reference (an app-specific password)                 |
| `sources.<name>.writable`           | `false` | `caldav` only: allow `calendar_create_event`                         |
| `sources.<name>.path`               | —       | `ics_file` path                                                      |
| `sources.<name>.allow_http`         | `false` | Allow `http://` for a server on your own network                     |

Source names may use letters, digits, `.`, `_`, and `-`.

## Tests

```bash
pytest tests/unit -k "calendar or dav"
pytest -m integration tests/integration/test_calendar_dav_server.py
```

The integration tests start a fake CalDAV server on `127.0.0.1` with an ephemeral port; no real
account or outside network is involved.
