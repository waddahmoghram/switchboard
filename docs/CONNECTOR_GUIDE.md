# Connector guide

A connector is a package at `src/switchboard/connectors/<name>/` that turns one family of services (mail, calendars, money, …) into **tools**, **events**, **jobs**, and **services**.
Connectors never import each other.
They cooperate only through the core: events on the bus and named services.

Payloads and tool arguments use JavaScript Object Notation (JSON).
Database migrations use Structured Query Language (SQL).
Here, tier names such as `READ` and `ACT` are literal policy identifiers.

## Module contract

| Name                              | Required | Meaning                                                                                        |
| --------------------------------- | -------- | ---------------------------------------------------------------------------------------------- |
| `NAME: str`                       | Yes      | Equals the key in `switchboard.app.KNOWN_CONNECTORS`.                                          |
| `MIGRATIONS`                      | No       | `list[tuple[int, Sequence[str]]]`: versioned single SQL statements; tables prefixed `<name>_`. |
| `register(ctx, registry) -> None` | Yes      | Add tools, subscribe to events, schedule jobs, provide services. **No network activity here.** |
| `doctor(ctx) -> list[str]`        | No       | Human-readable problems, for example a secret reference that does not resolve. Never secrets.  |
| `add_cli(subparsers) -> None`     | No       | Extra subcommands; set `set_defaults(app_handler=fn)` where `fn(args, app) -> int`.            |
| `retention` submodule             | No       | `<name>/retention.py` with `purge(...)` and optional `EXPORT_TABLES`; see the retention hooks. |

`register` runs on every command, so it must be fast and offline.
Resolve secrets lazily inside handlers and jobs with `ctx.secret(reference)`.
Read settings with `ctx.connector_config(NAME)`, which returns the `[connectors.<name>]` table.

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

* Tiers: **READ** retrieves data and may fetch or cache it; **DRAFT** saves prepared content, imports a local ledger, or notifies configured personal channels; **ACT** sends, creates, or changes outside data; **SPEND** moves money.
  The policy allows READ and DRAFT, asks for ACT, and denies SPEND unless configured otherwise.
* Schemas must bound everything: `maxLength` on strings, `maxItems` on arrays, `minimum`/`maximum` on numbers.
  The core validator supports `type`, `enum`, `const`, `properties`, `required`, `additionalProperties` (boolean), `propertyNames`, `minProperties`, `maxProperties`, `items`,
  `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`, `minItems`, and `maxItems`.
  Non-finite numbers are invalid.
  Registration rejects unsupported keywords and malformed definitions; it does not silently ignore a misspelled bound.
  This is still not a complete schema engine.
  Composition and references are refused.
  Recognized annotations such as `format`, plus `x-` extension metadata, are not assertions.
* ACT tools that contact people set `recipients=lambda args: [...]` so `allow_recipients` works.
  Money-related tools set `amount=lambda args: Decimal(...)` so `max_amount` works.
* ACT and SPEND tools set `summarize=lambda args: "..."`, one line a human can judge at approval time:
  who, what, when, how much.
* Raise `switchboard.errors.ToolError("short reason")` only when the requested effect **did not happen**: invalid input, a refusal by the provider or device, or a failure before anything was sent.
  Messages must not contain secrets, full message bodies, or account numbers.
* When a write may have reached the provider anyway, raise `switchboard.errors.UncertainOutcomeError`, or return a report with a top-level `"effect_unknown": true`.
  Typical cases are a connection that drops or times out after sending, an HTTP 408 or 5xx reply, and a success reply you cannot read.
  The action journal then records the call as `uncertain` and refuses an identical repeat until the operator reconciles it.
  Never retry a write yourself.
  Any other exception is reported by type only and is also recorded as `uncertain`.
  The rules are in [the contract](../contracts/README.md#classifying-a-result).
* Tools that read back state after a write report a boolean `confirmed`: `true` only when the readback matched the request.
  A readback is not proof of a physical effect; say so in the report.
* Minimize returned data: cap bodies (for example, 4,000 characters) and say when you truncated.

### Worked mistake: an unenforced length limit

Suppose your hook accepts a message.
The intended property definition is:

```python
{"type": "string", "maxLength": 80}
```

`maxLenght` looks plausible but is not the same keyword.
Previously it was ignored, so a tool author could think a limit existed when it did not.
Now `Tool.__post_init__` calls `check_schema` and refuses the definition before the tool becomes callable.
A nested typo identifies the property path in the error.
This catches a definition error; it does not prove that the handler correctly uses a valid message.

Follow `check_schema`, `validate`, and `_check_string` in [the validator](../src/switchboard/jsonschema.py), then inspect the authored cases in [the schema tests](../tests/unit/test_core_jsonschema.py).
A separate trap remains intentional:
`"format": "email"` is metadata, not address validation.
Use the existing mail address helpers or implement and test an actual assertion before depending on it.

## Writing automation rules

Start with one event and one action, not an unattended workflow.
For example, after configuring the finance and notification connectors, this rule requests a notification for a pending expense of at least 100 in United States dollars:

```toml
[[rules]]
name = "Large pending charge"
on = "finance.transaction"
[rules.match]
"payload.amount" = { lte = -100 }
"payload.currency" = "USD"
"payload.pending" = true
[[rules.do]]
tool = "notify_self"
args = { title = "Pending charge", message = "{payload.amount} {payload.currency} at {payload.merchant}" }
```

Expenses are negative, so `gte = 100` would select positive amounts instead.
The currency condition matters: there is no automatic conversion before comparing amounts.
Matching an imported alert is not proof that a bank has settled the charge.

`load_rules` in [the rule engine](../src/switchboard/rules.py) checks definitions before events are processed:

* Rule fields are `name`, `on`, `match`, and `do`.
  There is no `enabled` field; remove or comment out the whole rule to disable it.
  `enabled = false` is refused, not silently treated as an active rule.
* Action fields are `tool`, `args`, and the boolean `stop_on_error`.
* Condition tables accept `equals`, `contains`, `regex`, `in`, `exists`, `gte`, and `lte`.
  Unknown operators,
  empty condition tables, invalid regular expressions, and invalid operand types are refused.
* Numeric thresholds must be finite numbers, not quoted numbers or booleans.
  Equality keeps booleans distinct from numbers, so `true` does not count as `1`.
* `exists` checks for a non-null value: missing and explicitly null values both fail `exists = true`.
* Regular-expression syntax is checked, not execution cost.
  A user-authored expression can still backtrack badly on external text; avoid nested repetition.

Read [the pending-action case](./ARCHITECTURE.md#worked-case-a-pending-action-is-not-a-completed-workflow) before adding a second action.
The list is not an automatically awaited workflow: a later notification must not claim an earlier action completed merely because it was submitted.
The corresponding cases in [the rule tests](../tests/unit/test_core_rules.py) are authored regression tests, not runtime evidence from this host.

## Events

```python
ctx.events.emit(NAME, "mail.received", payload, dedupe_key=f"mail:{account}:{mailbox}:{uidvalidity}:{uid}")
ctx.events.subscribe("mail.received", "finance.alerts", handler)  # handler(event) -> None
```

Payloads are JSON objects with no secrets, normally under about 20 KB (verified webhook JSON may be larger, up to the hook's body limit).
A `dedupe_key` makes polling idempotent.
Subscribers run before user rules and may emit derived events.
Run one event processor at a time (`switchboard run`); events are not claimed across processes in v0.1.
Well-known event types:

| Type                      | Emitted by | Representative payload keys                                                                                                                           |
| ------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mail.received`           | mail       | account, mailbox, uid, message_id, from, to, cc, subject, date, text, has_attachments, attachments                                                    |
| `webhook.received`        | webhooks   | hook, received_at, content_type, size, headers (safe subset), json or text                                                                            |
| `finance.transaction`     | finance    | account, date, amount, currency, merchant, pending, source, category                                                                                  |
| `finance.budget_exceeded` | finance    | category, month, spent, limit, currency                                                                                                               |
| `shopping.order`          | shopping   | merchant, order_id, total, currency, source_message_id                                                                                                |
| `shopping.shipment`       | shopping   | merchant, order_id, carrier, tracking_number, tracking_url, status                                                                                    |
| `shopping.price_drop`     | shopping   | url, title, price, currency, target, previous                                                                                                         |
| `calendar.upcoming`       | calendar   | calendar, uid, summary, start, end, location, minutes_until                                                                                           |
| `telegram.message`        | telegram   | id, chat_id, user_id, message_id, untrusted_content; body stays in the inbox                                                                          |
| `linkedin.imported`       | linkedin   | file, datasets, records, inserted, historical                                                                                                         |
| `linkedin.draft_created`  | linkedin   | draft_id, draft_hash, author_urn, visibility, characters                                                                                              |
| `linkedin.post_published` | linkedin   | draft_id, draft_hash, author_urn, visibility, post_urn                                                                                                |
| `whatsapp.imported`       | whatsapp   | file, records, inserted, historical, chat_label, date_order, timezone_context, archive_member                                                         |
| `tasks.changed`           | tasks      | id, revision, status                                                                                                                                  |
| `tasks.deleted`           | tasks      | id, revision, deleted_at                                                                                                                              |
| `tasks.reminder_due`      | tasks      | id, revision, title, due_at, remind_at, untrusted_content, delivery                                                                                   |
| `monitoring.changed`      | monitoring | name, kind, status, error, reachable, ready, measurements, report, checked_at, changed_at, revision, previous_status                                  |
| `monitoring.alert`        | monitoring | Same bounded observation fields; alert-worthy state transition                                                                                        |
| `wled.action_result`      | wled       | device, target_digest, operation, attempted, requested, status, confirmed, confirmation, effect_unknown, observation, stale, physical_output_verified |
| `home.state_changed`      | home       | entity_id, previous, current, observed_at, revision, source (opaque server digest), history_complete                                                  |

`text` in `mail.received` is plain text extracted from the message, capped at 20,000 characters.
`wled.action_result` follows an attempted write, not a pending approval or failed preflight.
Its `confirmed` field describes matched software readback; `physical_output_verified` remains false.
An event-storage failure after a write does not prove that the write failed, and must not trigger a blind resend.
`home.state_changed` contains bounded previous/current software observations.
Its first baseline is silent and `history_complete` is false; elapsed-time staleness is displayed in polling status rather than fabricated as an observed device transition.

## Services

Named services let one connector use another's capability without importing it:

| Name             | Provided by | Interface                                                                                                     |
| ---------------- | ----------- | ------------------------------------------------------------------------------------------------------------- |
| `mail.drafts`    | mail        | `save_draft(message: EmailMessage, account: str \| None = None) -> str` (returns a draft reference)           |
| `mail.sender`    | mail        | `send(message: EmailMessage, account: str \| None = None) -> str` (returns the Message-ID)                    |
| `notify`         | notify      | `send(title: str, message: str, *, priority: str = "default", url: str \| None = None) -> list[str]`          |
| `telegram.inbox` | telegram    | `TelegramService`: `poll()`, `status(args)`, `messages(args)`, `read(args)`, `mark_read(args)`, `reply(args)` |
| `tasks.local`    | tasks       | `TaskService`: local task handlers and `reminders_job()`; internal calls do not dispatch policy               |
| `monitoring`     | monitoring  | Cached status, named checks and `poll()` returning checked/changed/events_queued/record_errors counts         |
| `home`           | home        | Internal read/control/poll handlers; caller must still use the dispatcher for external actions                |

Look services up when a handler runs (`ctx.service("mail.drafts")`), not in `register`, so the order in which connectors register does not matter.
Raise `ToolError` if a needed service is absent.
Services are trusted internal calls, not separately dispatched tools.
Their caller must apply the appropriate policy and account restrictions; setting a policy on `mail_send` alone does not gate every internal use of `mail.sender`.

`mail.sender.send` and `mail.drafts.save_draft` raise `UncertainOutcomeError` when the connection fails during submission.
Let it propagate, or report your own unknown outcome; never turn it into a definite failure.
`notify.send` returns only the channels that accepted the note; channels whose outcome is unknown are logged and left out, and nothing is resent.
A service can be called from another connector's job or tool, so it must not rely on its own connector's lane for mutual exclusion.

## Storage, web requests, and scheduling

* `with ctx.db.session() as conn:` gives an autocommit connection; wrap multi-statement writes in `with transaction(conn):` from `switchboard.db`.
  Use parameterized SQL only.
* To commit source state and an event together, call `ctx.events.emit(..., connection=conn)` inside that transaction.
  Do not open a second write connection with the default form while holding the first transaction.
  This is local atomicity, not exactly-once external delivery.
* Historical text imports can use `connectors._archive`: owned private directories, bounded regular-file reads, non-extracting archive reads, and snapshot storage with summary events.
  The table prefix is validated separately; all record values are bound parameters.
  Historical imports must not imply a current user command or verified sender identity.
* `ctx.db.kv_get(NAME, key)` / `ctx.db.kv_set(NAME, key, value)` store cursors such as mail message identifiers.
* `ctx.http.request(method, url, headers=..., body=..., allow_http=False)` returns an `HttpResponse` with `status`, `url`, lowercase `headers`, `body`, `.ok`, `.text()`, and `.json()`.
  Non-2xx statuses are returned; network failures raise `HttpError` (`TransientHttpError` when repeating might help).
  Only pass `allow_http=True` from an explicit configuration flag (for example a Home Assistant server on your own network).
* Inside a tool call or a job run, `ctx.http` clamps each request to the time left before the deadline (`tool_timeout_seconds` or `job_timeout_seconds`) and sends nothing once it has passed.
  Other code is not interrupted: check `switchboard.execution.remaining()` before starting long local work.
* A read can repeat transient failures with `request_with_retries(ctx.http, "GET", url, retries=2)` from `switchboard.httpclient` (1 to 3 retries; GET and HEAD only).
  It repeats network failures and HTTP 408, 429, 502, 503, and 504 with a jittered backoff, honors a short `Retry-After`, and works with test fakes.
  Other methods are refused, because repeating a write could apply it twice.
* When an assistant or a web page chose the address, pass `public_only=True`: the host must resolve only to public addresses and the connection pins those numeric addresses without resolving again.
  It bypasses environment proxies, retains certificate checks for the original host, and returns redirects for vetting (`connectors/shopping/fetch.py` shows the pattern).
  Fakes record the flag, so tests can assert it.
* `ctx.scheduler.add("<name>.<job>", interval_seconds, func)`: at least 30 seconds.
  Jobs must catch per-account failures so one broken account does not stop the rest.
* Jobs run on worker threads, up to `job_workers` at once.
  A job holds its *lane*, by default the job name before the first dot, so it never runs alongside another job or a rule-triggered tool of the same connector; jobs of different connectors do run concurrently.
  Open a database session per call rather than sharing a connection between threads.
* Retention hooks are opt-in: `<name>/retention.py` provides `purge(ctx, *, older_than, dry_run) -> dict[str, int]` and may list its own tables in `EXPORT_TABLES` for `switchboard data export`.
  A hook runs offline, leaves open work alone, and changes nothing on a dry run.
  See [writing a retention hook](./DATA_LIFECYCLE.md#writing-a-retention-hook).

## Security rules

* Never log, return, or raise secrets.
  Do not log addresses that carry tokens (Telegram puts its bot token in the request path).
* External content (mail, web pages, webhook bodies, file imports) is untrusted data.
  Never execute it,
  follow links automatically, or let it choose tools.
* Prefer read-only protocol operations: mail `EXAMINE` and `BODY.PEEK`, calendar `REPORT` before `PUT`.
* Respect provider rate limits, `robots.txt` for web pages, and `Retry-After` headers.
* Restrict file-reading tools to directories you configure; never accept arbitrary paths from an assistant.

## Tests

* Unit tests: `tests/unit/test_<connector>_*.py`; integration tests: `tests/integration/test_<connector>_*.py` marked `@pytest.mark.integration`; fixtures: `tests/fixtures/<connector>/`.
* No real accounts or outside network.
  Use `tests.fakes.FakeHttp`, local servers bound to `127.0.0.1` on port 0, or monkeypatched standard-library classes.
* Synthetic data only: no real names, addresses, card or account numbers.
* The `make_app` fixture in `tests/conftest.py` builds a full app with a temporary data directory and `FakeHttp`; pass the configuration dictionary you need.
