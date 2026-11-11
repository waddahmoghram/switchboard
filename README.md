# Switchboard

Self-hosted hooks that let your AI assistant work with **your own** mail, calendars, contacts, bank data,
orders, notifications, webhooks, and home devices — without paying an aggregator such as Plaid or an
automation service such as Zapier, and without handing them your credentials.

Switchboard is a single Python package with **no runtime dependencies**. It speaks open protocols directly
(IMAP, SMTP, CalDAV, CardDAV, iCalendar, Open Financial Exchange files, HTTPS APIs you own), exposes the
results to assistants through the Model Context Protocol (MCP), and puts a **policy gate** in front of
everything: assistants can read and draft freely, must ask before acting, and can never move money.

> Status: v0.1, written but **not yet run**. The test suite (549 test functions) and static checks were
> authored and passed linting and type checking, but no test or connector was executed on the authoring
> machine. Run the tests on your own computer first; see [Deployment](./docs/DEPLOYMENT.md).

## What it connects

| Connector  | Talks to                                                                     | Tools (tier)                                                                                                                                                 |
| ---------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mail`     | Any IMAP/SMTP provider; app passwords or OAuth                               | `mail_accounts`, `mail_mailboxes`, `mail_search`, `mail_read` (read); `mail_draft` (draft); `mail_send` (act)                                                |
| `calendar` | Secret iCalendar feeds, CalDAV, `.ics` files, email invites                  | `calendar_sources`, `calendar_agenda`, `calendar_free_slots` (read); `calendar_propose_event` (draft); `calendar_create_event`, `calendar_send_invite` (act) |
| `contacts` | CardDAV and vCard files                                                      | `contacts_sources`, `contacts_search` (read)                                                                                                                 |
| `finance`  | Bank OFX/QFX/CSV exports and bank alert emails                               | `finance_accounts`, `finance_transactions`, `finance_summary`, `finance_recurring` (read); `finance_import_inbox` (draft)                                    |
| `shopping` | Order and shipping emails; product pages you choose                          | `shopping_orders`, `shopping_shipments`, `shopping_watch_list`, `shopping_check_prices` (read); `shopping_watch_add`, `shopping_watch_remove` (draft)        |
| `notify`   | ntfy, Telegram, and email to yourself                                        | `notify_self` (draft: it can only reach channels you configured)                                                                                             |
| `webhooks` | Signed inbound webhooks (GitHub, Microsoft Graph, Google Calendar, your own) | `webhooks_recent` (read)                                                                                                                                     |
| `home`     | Home Assistant's REST API                                                    | `home_states` (read); `home_call_service` (act)                                                                                                              |
| core       | Switchboard itself                                                           | `switchboard_status`, `approvals_pending`, `approvals_status` (read)                                                                                         |

There is no "buy" or "pay" tool. Price watches tell you when an item hits your target, and you buy it
yourself. The reasons are in [Tradeoffs](./docs/TRADEOFFS.md#money-movement-and-checkout).

## How it fits together

* **Assistants** call tools over MCP on standard input and output; no network port is opened.
* **The runner** (`switchboard run`) polls mailboxes, calendars, and price watches, turns what it finds into
  events, and runs your automation rules — a local "if this, then that".
* **You** approve pending actions from the terminal, flip the kill switch, and verify the audit log.

Every call, from any of those three, goes through the same dispatcher: argument validation, the policy
decision, an audit record, then execution. See [Architecture](./docs/ARCHITECTURE.md).

## Quick start

```bash
uv sync
uv run switchboard init          # starter config at ~/.config/switchboard/config.toml (mode 600)
uv run switchboard doctor
uv run switchboard tools
uv run pytest                    # run the tests on your machine first
```

Then enable one connector in the configuration, store its credential in your password manager, reference it
(`password = "cmd:pass show switchboard/mail"`), and connect an assistant (examples for Claude Desktop,
OpenClaw, and Hermes Agent are in [Deployment](./docs/DEPLOYMENT.md#connect-an-assistant)).

## Safety model in one table

| Tier    | Default | Meaning                                         |
| ------- | ------- | ----------------------------------------------- |
| `read`  | allow   | Look at data; no outside effect                 |
| `draft` | allow   | Prepare something you finish yourself           |
| `act`   | ask     | Send, create, or change something outside       |
| `spend` | deny    | Move money (no such tool exists)                |

Plus: recipient allowlists, per-tool rate limits, amount caps, an exposure list for assistants, approvals that
bind the exact arguments, a kill switch, secret references instead of secrets, owner-only file permissions,
and a hash-chained audit log.

## Documentation

* [Architecture](./docs/ARCHITECTURE.md) — components, the call path, trust boundaries
* [Tradeoffs](./docs/TRADEOFFS.md) — what doing it yourself costs, connector by connector
* [Deployment](./docs/DEPLOYMENT.md) — install, test, connect assistants, run as a service, revoke
* [Connector guide](./docs/CONNECTOR_GUIDE.md) — how to write a new connector
* Connector pages: [mail](./docs/connectors/mail.md), [calendar](./docs/connectors/calendar.md),
  [contacts](./docs/connectors/contacts.md), [finance](./docs/connectors/finance.md),
  [shopping](./docs/connectors/shopping.md), [notify](./docs/connectors/notify.md),
  [webhooks](./docs/connectors/webhooks.md), [home](./docs/connectors/home.md)

## License

No license has been chosen yet. Until one is added, all rights are reserved by the author.
