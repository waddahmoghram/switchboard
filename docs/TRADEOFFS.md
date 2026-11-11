# Tradeoffs

Building your own hooks instead of renting them is cheaper and keeps your credentials at home, but it is not
free in effort or capability. This page states what you give up and what you get, area by area. Provider
facts were checked on 2026-11-11 and are cited on each connector's page.

## The big trade: do it yourself versus pay an intermediary

| Need                     | What a paid intermediary gives you                                 | What Switchboard does instead                                                          | What it costs you                                                     |
| ------------------------ | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Bank data                | Live balances and transactions from thousands of institutions      | Imports the OFX, QFX, or CSV files your bank lets you download, plus bank alert emails | No live balances; periodic downloads; per-bank alert rules            |
| Automation across apps   | Thousands of maintained integrations and a hosted workflow runner  | Open protocols, a local event queue, and `[[rules]]`                                   | You maintain connectors; far fewer integrations                       |
| Sign-in to big providers | A verified OAuth app and long-lived tokens                         | App passwords where providers allow them; your own OAuth app registration otherwise    | Some providers require OAuth; Google testing tokens expire weekly     |
| Push notifications       | Hosted endpoints that receive provider webhooks                    | Polling by default; an optional signed webhook receiver behind your own tunnel         | Slower updates, or running a public endpoint yourself                 |
| Shopping and payment     | Agent checkout with scoped payment tokens                          | Order tracking from email and polite price watches; you buy                            | No automated buying                                                   |

What you gain: no subscription, no third party storing your bank or mail credentials, code you can read
(no runtime dependencies), one audit log for everything, and a policy you set.

## Mail

* **App passwords versus OAuth.** IMAP with an app password is the durable free path for Gmail, iCloud, and
  Fastmail, but an app password opens the *whole* mailbox and every protocol it covers. OAuth narrows scope,
  yet Google issues refresh tokens that expire after 7 days while an OAuth app stays in "Testing", and
  publishing an app with restricted Gmail scopes requires verification. Outlook.com no longer accepts basic
  authentication, so it needs OAuth with your own free app registration. Details: [mail](./connectors/mail.md#app-passwords-versus-oauth).
* **Polling, not IMAP IDLE.** Simpler and more robust across providers; new mail arrives within one poll
  interval (5 minutes by default) instead of seconds.
* **Drafts before sends.** `mail_draft` saves to your Drafts folder and you press Send. `mail_send` exists but is
  off per account and still asks for approval when on.

## Calendars and contacts

* **Secret iCalendar addresses** need no OAuth and work with Google, but they are read-only and act like
  passwords: anyone holding the address can read the calendar, so Switchboard stores them as secret references.
* **CalDAV and CardDAV** give read and write access with app passwords on iCloud and Fastmail (Fastmail's
  Basic plan excludes them); Google's CalDAV and CardDAV accept only OAuth.
* **Writing to Google without OAuth:** `calendar_propose_event` saves an email invitation as a draft; sending it
  to yourself adds the event. You stay in the loop, at the cost of one manual step.
* **A recurrence subset.** Common daily, weekly, monthly, and yearly rules expand correctly, including
  daylight-saving changes; rare parts (`BYSETPOS`, `BYWEEKNO`, `BYYEARDAY`, hourly rules) fall back to the first
  occurrence with a warning. Details: [calendar](./connectors/calendar.md#tradeoffs-and-limits).

## Money: bank data without an aggregator

* **Files and alerts, not scraping.** Logging in to your bank's website with your password from a script breaks
  most banks' terms, trips multi-factor prompts, and puts your credentials in a program. Switchboard never does it.
* **No live balances.** A statement file is as fresh as your last download. Alert emails fill the gap with pending
  entries that later match the posted transaction, so each purchase counts once.
* **OFX Direct Connect** (the protocol older desktop finance programs used) has largely disappeared and would
  require storing your bank password; it is not implemented.
* **Low-cost bridges exist** (SimpleFIN Bridge charges a small yearly fee), but they are still paid
  intermediaries. In the US, the Consumer Financial Protection Bureau's personal financial data rights rule,
  which would require banks to share data with authorized third parties, is stayed and under reconsideration.
* **Privacy:** full account numbers are never stored; the ledger keeps the last four digits and a salted hash.
  Details: [finance](./connectors/finance.md#tradeoffs-and-limits).

## Money movement and checkout

There is no tool that pays, transfers, or buys, and the `spend` tier denies by default. Reasons:

* There is no free, terms-compliant checkout interface for ordinary retailers; automating a browser to buy would
  mean storing payment credentials and fighting bot checks.
* Agent shopping is still unreliable: in Visa's published benchmark, models passed 57.2% of shopping tasks, and
  54% of failed runs still ended in a purchase.
* Approving each purchase in a terminal would be no faster than buying it yourself.

What you get instead: price watches that respect `robots.txt`, `Retry-After`, and a minimum interval per page,
then tell you (through `notify_self` and a rule) when an item reaches your target, with the link. Prices shown only
by JavaScript are invisible to a plain fetch. Details: [shopping](./connectors/shopping.md#why-there-is-no-buy-tool).

## Notifications, webhooks, and home

* **ntfy on the public server** is free, but a topic name is effectively a password; Switchboard treats it as a
  secret. Self-hosting ntfy removes that exposure. Telegram puts the bot token in the request path, so no URL is
  ever logged.
* **Inbound webhooks need a public HTTPS endpoint.** The receiver binds to `127.0.0.1`; exposing it means running a
  tunnel such as Tailscale Funnel or Cloudflare Tunnel, and every public endpoint attracts probing. Each hook is
  verified (signatures with timestamps and replay protection, or the provider's own scheme). Polling avoids all
  of this and is the default. Details: [webhooks](./connectors/webhooks.md#push-or-poll).
* **Home Assistant** is reachable only for entities you expose, and locks, alarms, covers (including garage doors),
  and cameras are denied unless you list them explicitly.

## Architecture choices

| Choice                         | Benefit                                                         | Cost                                                                                                 |
| ------------------------------ | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Standard library only          | Nothing to audit beyond this code; no supply-chain updates      | Hand-written parsers (iCalendar, recurrence, OFX, JSON Schema subset) with edge cases                |
| MCP over standard input/output | No listening port; the assistant starts and stops the server    | One process per assistant; calls are handled one at a time                                           |
| Dual-era MCP                   | Works with clients on `2026-07-28` and on `2025-11-25` or older | Two code paths to keep current as the protocol evolves                                               |
| One SQLite file                | Simple backups; write-ahead logging lets processes share it     | Events are not claimed across processes; a lock file allows one `switchboard run` per data directory |
| Approvals in the terminal      | Nothing new to expose; exact arguments are bound                | You must be at a terminal; phone approvals are future work and need careful design                   |
| Hash-chained audit log         | Edits, deletions, and reordering are detected                   | Tamper-evident, not tamper-proof; keep a copy of the latest hash elsewhere                           |
| Polling with long intervals    | Polite to providers; no public endpoint                         | Minutes of delay instead of seconds                                                                  |
| Python 3.11+ on Linux or macOS | `tomllib`, `zoneinfo`, and `fcntl` in the standard library      | Windows is untested                                                                                  |

## What this version cannot do

* Show live bank balances or reach banks that offer no downloads or alert emails.
* Use Google's mail, calendar, or contacts APIs without OAuth (and weekly re-consent while your app is in testing).
* Buy anything, move money, or fill payment forms.
* Guarantee safety against prompt injection: it limits what an injected instruction can achieve, it does not
  make the assistant immune.
* Read prices that appear only after JavaScript runs.
* Claim events across several runner processes, or approve actions from a phone.

## Testing status

All tests were written alongside the code and checked statically (Ruff linting and formatting, mypy type
checking of the package and the tests), but none were executed on the authoring machine. Expect a first run on
your own computer to surface some failures in time-, locale-, or platform-sensitive tests; the connector pages
list the tests most likely to need attention.
