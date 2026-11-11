# Connector guide

A connector is a package at `src/switchboard/connectors/<name>/` that turns one family of services
(mail, calendars, money, …) into **tools**, **events**, **jobs**, and **services**. Connectors never
import each other. They cooperate only through the core: events on the bus and named services.

## Module contract

| Name                              | Required | Meaning                                                                                        |
| --------------------------------- | -------- | ---------------------------------------------------------------------------------------------- |
| `NAME: str`                       | Yes      | Equals the key in `switchboard.app.KNOWN_CONNECTORS`.                                          |
| `MIGRATIONS`                      | No       | `list[tuple[int, Sequence[str]]]`: versioned single SQL statements; tables prefixed `<name>_`. |
| `register(ctx, registry) -> None` | Yes      | Add tools, subscribe to events, schedule jobs, provide services. **No network I/O here.**      |
| `doctor(ctx) -> list[str]`        | No       | Human-readable problems, for example a secret reference that does not resolve. Never secrets.  |
| `add_cli(subparsers) -> None`     | No       | Extra subcommands; set `set_defaults(app_handler=fn)` where `fn(args, app) -> int`.            |

`register` runs on every command, so it must be fast and offline. Resolve secrets lazily inside
handlers and jobs with `ctx.secret(reference)`. Read settings with `ctx.connector_config(NAME)`, which
returns the `[connectors.<name>]` table.

## Tools

```python
from switchboard.policy import Tier
from switchboard.tools import Tool

registry.add(Tool(
    name="mail_search",                 # <connector>_<verb>, lowercase, ≤ 64 characters
    description="Search one mailbox.",  # written for the assistant; say what it will NOT do
    tier=Tier.READ,                     # READ, DRAFT, ACT, or SPEND
    input_schema={...},                 # JSON Schema object; additionalProperties false
    handler=search,                     # (args: dict) -> JSON-serializable value
    title="Search mail",
    connector=NAME,
))
```

* Tiers: **READ** looks only; **DRAFT** prepares something you finish yourself (a mail draft, a local
  ledger import); **ACT** sends, creates, or changes outside data; **SPEND** moves money. The policy
  allows READ and DRAFT, asks for ACT, and denies SPEND unless configured otherwise.
* Schemas must bound everything: `maxLength` on strings, `maxItems` on arrays, `minimum`/`maximum` on
  numbers. The core validator supports `type`, `enum`, `const`, `properties`, `required`,
  `additionalProperties` (boolean), `items`, `minLength`, `maxLength`, `pattern`, `minimum`,
  `maximum`, `minItems`, and `maxItems`.
* ACT tools that contact people set `recipients=lambda args: [...]` so `allow_recipients` works.
  Money-related tools set `amount=lambda args: Decimal(...)` so `max_amount` works.
* ACT and SPEND tools set `summarize=lambda args: "..."`, one line a human can judge at approval time:
  who, what, when, how much.
* Raise `switchboard.errors.ToolError("short reason")` for expected failures. Messages must not contain
  secrets, full message bodies, or account numbers. Any other exception is reported by type only.
* Minimize returned data: cap bodies (for example, 4,000 characters) and say when you truncated.

## Events

```python
ctx.events.emit(NAME, "mail.received", payload, dedupe_key=f"mail:{account}:{mailbox}:{uidvalidity}:{uid}")
ctx.events.subscribe("mail.received", "finance.alerts", handler)  # handler(event) -> None
```

Payloads are JSON objects with no secrets, normally under about 20 KB (verified webhook JSON may be larger,
up to the hook's body limit). A `dedupe_key` makes polling idempotent. Subscribers run before user rules
and may emit derived events. Run one event processor at a time (`switchboard run`); events are not claimed
across processes in v0.1. Well-known event types:

| Type                      | Emitted by | Payload keys                                                                                       |
| ------------------------- | ---------- | -------------------------------------------------------------------------------------------------- |
| `mail.received`           | mail       | account, mailbox, uid, message_id, from, to, cc, subject, date, text, has_attachments, attachments |
| `webhook.received`        | webhooks   | hook, received_at, content_type, size, headers (safe subset), json or text                         |
| `finance.transaction`     | finance    | account, date, amount, currency, merchant, pending, source, category                               |
| `finance.budget_exceeded` | finance    | category, month, spent, limit, currency                                                            |
| `shopping.order`          | shopping   | merchant, order_id, total, currency, source_message_id                                             |
| `shopping.shipment`       | shopping   | merchant, order_id, carrier, tracking_number, tracking_url, status                                 |
| `shopping.price_drop`     | shopping   | url, title, price, currency, target, previous                                                      |
| `calendar.upcoming`       | calendar   | calendar, uid, summary, start, end, location, minutes_until                                        |

`text` in `mail.received` is plain text extracted from the message, capped at 20,000 characters.

## Services

Named services let one connector use another's capability without importing it:

| Name          | Provided by | Interface                                                                                              |
| ------------- | ----------- | ------------------------------------------------------------------------------------------------------ |
| `mail.drafts` | mail        | `save_draft(message: EmailMessage, account: str \| None = None) -> str` (returns a draft reference)    |
| `mail.sender` | mail        | `send(message: EmailMessage, account: str \| None = None) -> str` (returns the Message-ID)             |
| `notify`      | notify      | `send(title: str, message: str, *, priority: str = "default", url: str \| None = None) -> list[str]`   |

Look services up when a handler runs (`ctx.service("mail.drafts")`), not in `register`, so the
order in which connectors register does not matter. Raise `ToolError` if a needed service is absent.

## Storage, HTTP, and scheduling

* `with ctx.db.session() as conn:` gives an autocommit connection; wrap multi-statement writes in
  `with transaction(conn):` from `switchboard.db`. Use parameterized SQL only.
* `ctx.db.kv_get(NAME, key)` / `ctx.db.kv_set(NAME, key, value)` store cursors such as IMAP UIDs.
* `ctx.http.request(method, url, headers=..., body=..., allow_http=False)` returns an
  `HttpResponse` with `status`, `url`, lowercase `headers`, `body`, `.ok`, `.text()`, and `.json()`.
  Non-2xx statuses are returned; network failures raise `HttpError`. Only pass `allow_http=True` from
  an explicit configuration flag (for example a Home Assistant server on your own network).
* When an assistant or a web page chose the URL, pass `public_only=True`: the host must resolve only
  to public addresses, and redirects come back as 3xx responses for you to vet hop by hop
  (`connectors/shopping/fetch.py` shows the pattern). Fakes record the flag, so tests can assert it.
* `ctx.scheduler.add("<name>.<job>", interval_seconds, func)`: at least 30 seconds. Jobs must catch
  per-account failures so one broken account does not stop the rest.

## Security rules

* Never log, return, or raise secrets. Do not log URLs that carry tokens (Telegram's Bot API puts the
  token in the path).
* External content (mail, web pages, webhook bodies, file imports) is untrusted data. Never execute it,
  follow links automatically, or let it choose tools.
* Prefer read-only protocol modes: IMAP `EXAMINE` and `BODY.PEEK`, CalDAV `REPORT` before `PUT`.
* Respect provider rate limits, `robots.txt` for web pages, and `Retry-After` headers.
* Restrict file-reading tools to directories you configure; never accept arbitrary paths from an
  assistant.

## Tests

* Unit tests: `tests/unit/test_<connector>_*.py`; integration tests: `tests/integration/test_<connector>_*.py`
  marked `@pytest.mark.integration`; fixtures: `tests/fixtures/<connector>/`.
* No real accounts or outside network. Use `tests.fakes.FakeHttp`, local servers bound to
  `127.0.0.1` on port 0, or monkeypatched standard-library classes.
* Synthetic data only: no real names, addresses, card or account numbers.
* The `make_app` fixture in `tests/conftest.py` builds a full app with a temporary data directory and
  `FakeHttp`; pass the configuration dictionary you need.
