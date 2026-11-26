# Switchboard

Self-hosted hooks that let your personal assistant work with **your own** mail, calendars, contacts, bank data, orders, notifications, webhooks, home devices, local notes, tasks, and health checks — without paying an aggregator such as Plaid or an automation service such as Zapier, and without handing them your credentials.

Switchboard has a full Python reference implementation and a **C++20-first native backend** for selected local-first tools.
C99 and Rust clients share the C++ engine through a versioned C interface; they are not three independent ports or Python launchers.
See [native language coverage](./native/README.md#coverage).

The Python package has **no third-party Python runtime dependencies**; the native backend uses established transport, database, structured-data and cryptographic libraries.
Python uses standard mail, calendar, and contact protocols, bank export files, and direct service interfaces.
Both backends expose tools through the Model Context Protocol (MCP) with a **policy gate**: read and draft tools normally run immediately, act tools normally ask for approval, and no payment or checkout tool is implemented.

> Status: v0.1 prototype, written but **not yet run**. Tests are authored, not executed. Source and test files
> pass static formatting, lint, and type checks; those checks do not prove runtime behavior or provider
> compatibility. No application, test, or connector was executed on the authoring machine. Run the tests
> on your own computer first; see [Deployment](./docs/DEPLOYMENT.md).

**This is a prototype, not a sandbox or a verified bank integration.**
An assistant with shell access as your user can run approval commands, read your secrets, or edit the data directly.
The optional broker moves credentials, data, and approvals to a dedicated service account so that only separate operating-system accounts can decide; see [Isolation](./docs/ISOLATION.md).
Self-hosting also does not keep mail or financial results away from your assistant's model provider; use a local model and client if those results must stay on your machine.
See [the operating boundary](./docs/DEPLOYMENT.md#the-operating-boundary).

## What it connects

| Connector  | Talks to                                                                       | Tools (tier)                                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mail`     | Standard mail servers; app passwords or the OAuth authorization framework      | `mail_accounts`, `mail_mailboxes`, `mail_search`, `mail_read` (read); `mail_draft` (draft); `mail_send` (act)                                                |
| `calendar` | Secret iCalendar feeds, calendar servers, `.ics` files, email invites          | `calendar_sources`, `calendar_agenda`, `calendar_free_slots` (read); `calendar_propose_event` (draft); `calendar_create_event`, `calendar_send_invite` (act) |
| `contacts` | Contact synchronization servers and vCard files                                | `contacts_sources`, `contacts_search` (read)                                                                                                                 |
| `finance`  | Bank `.ofx`, `.qfx`, and `.csv` exports and bank alert emails                  | `finance_accounts`, `finance_transactions`, `finance_summary`, `finance_recurring` (read); `finance_import_inbox` (draft)                                    |
| `shopping` | Order and shipping emails; product pages you choose                            | `shopping_orders`, `shopping_shipments`, `shopping_watch_list`, `shopping_check_prices` (read); `shopping_watch_add`, `shopping_watch_remove` (draft)        |
| `notify`   | ntfy, Telegram, and email to yourself                                          | `notify_self` (draft: it can only reach channels you configured)                                                                                             |
| `webhooks` | Verified inbound webhooks (GitHub, Microsoft Graph, Google Calendar, your own) | `webhooks_recent` (read)                                                                                                                                     |
| `home`     | Home Assistant's service interface                                             | `home_states` (read); `home_call_service` (act)                                                                                                              |
| `telegram` | Allowlisted incoming bot messages and replies                                  | `telegram_status`, `telegram_messages`, `telegram_read` (read); `telegram_mark_read` (draft); `telegram_reply` (act)                                         |
| `linkedin` | Your downloaded exports and explicitly enabled own-profile publishing          | `linkedin_search`, `linkedin_read`, `linkedin_drafts` (read); `linkedin_import_export`, `linkedin_draft_post` (draft); `linkedin_publish_post` (act)         |
| `whatsapp` | Your manually exported personal chat snapshots                                 | `whatsapp_import_export` (draft); `whatsapp_search`, `whatsapp_read` (read); no live messaging                                                               |
| core       | Switchboard itself                                                             | `switchboard_status`, `approvals_pending`, `approvals_status` (read)                                                                                         |

### Local-first extensions

| Connector                                     | Purpose                                                                                                                  | Boundary                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| [home](./docs/connectors/home.md)             | Typed lighting, climate/fan and media controls, sensor/energy summaries, and state-change polling through Home Assistant | Explicit targets and safety limits; not arbitrary household automation          |
| [wled](./docs/connectors/wled.md)             | Direct local lighting status and approved absolute controls/presets                                                      | Fixed configured endpoints; no firmware or arbitrary command interface          |
| [shelly](./docs/connectors/shelly.md)         | Direct generation 2+ device/energy reads and opt-in approved safe switch controls                                        | Selected devices/components; not high-power appliance automation                |
| [documents](./docs/connectors/documents.md)   | Bounded read-only listing, search and reading of selected local notes                                                    | Private allowlisted text collections, not a whole-disk assistant                |
| [tasks](./docs/connectors/tasks.md)           | Local tasks, revisions, and one-shot reminder events                                                                     | Notification rules are separate from queueing; no task-provider synchronization |
| [monitoring](./docs/connectors/monitoring.md) | Fixed-target health, backup-report freshness and disk-capacity observations                                              | Read-only checks; not a backup, restore, shell or server-management tool        |

These are opt-in.
Home Assistant is a separate self-hosted device gateway; direct WLED and Shelly hooks do not require it.
Device compatibility, authentication, and exact capabilities depend on the selected model and firmware.
No device was contacted during authoring.

There is no "buy" or "pay" tool.
Price watches tell you when an item hits your target, and you buy it yourself.
The reasons are in [Tradeoffs](./docs/TRADEOFFS.md#money-movement-and-checkout).

## How it fits together

* **Assistants** call tools over MCP on standard input and output; no network port is opened.
* **The runner** (`switchboard run`) runs enabled polling and health jobs, queues due task reminders,
  turns observations into events, and runs your automation rules — a local "if this, then that".
* **You** approve pending actions from the terminal, flip the kill switch, and verify the audit log.
* **The action journal** records every draft and act call before its handler runs.
  A write whose outcome is unknown, such as a send whose connection dropped, is marked `uncertain`, and an identical repeat is refused until you check and record what happened with `switchboard actions resolve`.
  See [Recovery](./docs/RECOVERY.md).
* **Deadlines** bound each tool call (`tool_timeout_seconds`) and each polling-job run (`job_timeout_seconds`);
  the runner runs up to `job_workers` jobs at once, never two from the same connector.
* **The optional broker** (`switchboard broker serve`) serves the assistant and the operator on separate local sockets under a dedicated account; the assistant connects with `switchboard serve-mcp --broker SOCKET`.
* **Data lifecycle commands** create and verify backups, export or purge stored data under `[retention]`, and archive the audit log.
  See [Data lifecycle](./docs/DATA_LIFECYCLE.md).

Every tool call goes through the same dispatcher: argument validation, policy, audit, then execution.
Polling and connector-to-connector service calls are trusted application code, not additional dispatcher calls.
See [Architecture](./docs/ARCHITECTURE.md).

The Telegram connector is an assistant-pulled inbox/reply bridge, not a model runner: receiving a message does not start an assistant turn.
Your assistant must check the inbox through its tools or an operator-configured loop.
LinkedIn and WhatsApp imports are historical snapshots, not live personal-account access.
LinkedIn publishing is off until explicitly configured; live WhatsApp messaging is not implemented.

Likewise, a device accepting a command is not proof of its physical state, and a reminder event is not a delivered notification.
Read the returned verification/error state and each connector's limits before building a multi-step routine.

## Start here: no accounts

Run this on your own machine from a fresh checkout.
Start with the tests before enabling a connector or entering credentials.
Existing configuration is not overwritten by `init`; do not add `--force` merely to get past that warning.

```bash
uv sync
uv run pytest -m "not integration"
uv run pytest -m integration
uv run switchboard init          # starter config at ~/.config/switchboard/config.toml (mode 600)
uv run switchboard doctor
uv run switchboard call switchboard_status
```

With an unchanged starter configuration and a fresh data directory, the last command should report `status: "ok"` and data containing `connectors: []`, `tools: 4`, and `kill_switch: false`.
Its audit hash changes between calls; do not compare it with a fixed example.
This checks local assembly and the dispatcher, not mail delivery or provider sign-in.
These are expected results from source inspection, not commands executed on the authoring host.
`scripts/ci/validate.sh` runs every check stage, including the native builds, contract fixtures, fault tests, sanitizers and fuzzing; see [Validation](./docs/VALIDATION.md).

Stop here until that local path works.
Then enable **one** connector, store its credential in your password manager, and reference it (`password = "cmd:pass show switchboard/mail"`), and connect an assistant (examples for Claude Desktop, OpenClaw, and Hermes Agent are in [Deployment](./docs/DEPLOYMENT.md#connect-an-assistant)).

### Read one call before extending it

Follow `cmd_call` in [the command-line entry](./src/switchboard/cli.py), then `Dispatcher.call` in [the tool dispatcher](./src/switchboard/tools.py).
It validates arguments, asks `Policy.evaluate` in [the policy module](./src/switchboard/policy.py), records the request, and only then invokes the handler.
The `switchboard_status` handler is registered by `_register_core_tools` in [application assembly](./src/switchboard/app.py).
This is a small, account-free route through the actual code.

Next, read [the worked pending-action case](./docs/ARCHITECTURE.md#worked-case-a-pending-action-is-not-a-completed-workflow) before chaining actions.
For your first custom hook, use [the schema and rule examples](./docs/CONNECTOR_GUIDE.md#writing-automation-rules); the connector guide explains which mistakes are rejected at startup and which guarantees the automation engine does not provide.

## Safety model in one table

| Tier    | Default | Meaning                                           |
| ------- | ------- | ------------------------------------------------- |
| `read`  | allow   | Retrieve data; may contact services and cache it  |
| `draft` | allow   | Save prepared content or notify your own channels |
| `act`   | ask     | Send, create, or change something outside         |
| `spend` | deny    | Move money (no such tool exists)                  |

Plus: recipient allowlists, per-tool rate limits, amount caps, an exposure list for assistants, approvals that bind the exact arguments, a kill switch, secret references instead of secrets, owner-only file permissions,
a hash-chained audit log, deadlines on every call and job, and an action journal that refuses to repeat a write with an unknown outcome until you reconcile it.

Approvals recheck the current argument schema and policy.
Hourly limits include approved and in-flight calls; concurrent processes reserve capacity atomically.
These controls do not identify a human independently of the local user account.

## Documentation

* [C++ native backend](./native/README.md) — C++ preference, coverage differences, dependencies and own-machine builds
* [C and Rust bindings](./native/docs/bindings.md) — shared-engine design, ownership and errors
* [Native operations](./native/docs/operations.md) and [native isolation](./native/docs/isolation.md) — the native journal, deadlines, backups, and broker
* [Cross-engine contract](./contracts/README.md) — outcomes, journal states, deadlines, and the operator protocol that both engines share, with known differences
* [Architecture](./docs/ARCHITECTURE.md) — components, the call path, trust boundaries
* [Tradeoffs](./docs/TRADEOFFS.md) — what doing it yourself costs, connector by connector
* [Deployment](./docs/DEPLOYMENT.md) — install, test, connect assistants, run as a service, revoke
* [Recovery](./docs/RECOVERY.md) — the action journal, reconciling unknown outcomes, deadlines, and the runner
* [Isolation](./docs/ISOLATION.md) — the broker, separate service, assistant, and operator accounts
* [Data lifecycle](./docs/DATA_LIFECYCLE.md) — backups, restores, exports, retention, and audit archives
* [Validation](./docs/VALIDATION.md) — the off-host checks, pinned toolchains, fuzzing, and fault tests
* [Security policy](./SECURITY.md) — reporting a vulnerability privately, supported versions, and scope
* [Connector guide](./docs/CONNECTOR_GUIDE.md) — how to write a new connector
* Connector pages: [mail](./docs/connectors/mail.md), [calendar](./docs/connectors/calendar.md), [contacts](./docs/connectors/contacts.md), [finance](./docs/connectors/finance.md), [shopping](./docs/connectors/shopping.md), [notify](./docs/connectors/notify.md), [webhooks](./docs/connectors/webhooks.md), [home](./docs/connectors/home.md), [Telegram](./docs/connectors/telegram.md), [LinkedIn](./docs/connectors/linkedin.md), and [WhatsApp exports](./docs/connectors/whatsapp.md), [WLED](./docs/connectors/wled.md), [Shelly](./docs/connectors/shelly.md), [documents](./docs/connectors/documents.md), [tasks](./docs/connectors/tasks.md), and [monitoring](./docs/connectors/monitoring.md)

## License

Switchboard is released under the [MIT License](./LICENSE).
