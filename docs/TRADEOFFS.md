# Tradeoffs

Building your own hooks instead of renting them is cheaper and keeps your credentials at home, but it is not
free in effort or capability. This page states what you give up and what you get, area by area. Provider
facts were checked on 2026-11-11 and are cited on each connector's page.

The Model Context Protocol (MCP) is the implemented assistant interface, not proof of compatibility with
every assistant. Its request and tool schemas were checked against the official specification; no actual
client connection has been exercised here.

## The big trade: do it yourself versus pay an intermediary

| Need                     | What a paid intermediary gives you                                 | What Switchboard does instead                                                          | What it costs you                                                     |
| ------------------------ | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Bank data                | Live balances and transactions from thousands of institutions      | Imports `.ofx`, `.qfx`, or `.csv` downloads and bank alert emails                      | No live balances; periodic downloads; per-bank alert rules            |
| Automation across apps   | Thousands of maintained integrations and a hosted workflow runner  | Open protocols, a local event queue, and `[[rules]]`                                   | You maintain connectors; far fewer integrations                       |
| Sign-in to big providers | A verified OAuth app and long-lived tokens                         | App passwords where providers allow them; your own OAuth app registration otherwise    | Some providers require OAuth; Google testing tokens expire weekly     |
| Push notifications       | Hosted endpoints that receive provider webhooks                    | Polling by default; an optional verified webhook receiver behind your own tunnel       | Slower updates, or running a public endpoint yourself                 |
| Shopping and payment     | Agent checkout with scoped payment tokens                          | Order tracking from email and polite price watches; you buy                            | No automated buying                                                   |

What you gain: no aggregator subscription, code you can read, a shared audit log for tool calls, and a policy
you set. There are no third-party Python runtime packages, but the interpreter, operating system, development
tools, and providers still need maintenance. A hosted assistant still receives the tool results you expose.

## Mail

* **App passwords versus OAuth.** The Internet Message Access Protocol (IMAP) with an app password is one free
  route for Gmail, iCloud, and Fastmail, but an app password opens the *whole* mailbox and every protocol it
  covers. The OAuth authorization framework can narrow access, but this connector's Google mail scope is
  broad. Google issues refresh tokens that expire after 7 days while an OAuth app stays in "Testing", and
  widely distributing an app with restricted Gmail scopes can require verification; Google's personal-use
  exemption is described on the mail page. Outlook.com no longer accepts basic
  authentication, so it needs OAuth with your own free app registration. Details: [mail](./connectors/mail.md#app-passwords-versus-oauth).
* **Polling, not IMAP `IDLE`.** Mail is checked every 5 minutes by default. Delivery can take longer when a
  provider is unreachable, the runner is stopped, or a previous job is still running.
* **Drafts before sends.** `mail_draft` saves to your Drafts folder and you press Send. `mail_send` exists but is
  off per account and still asks for approval when on.

## Calendars and contacts

* **Secret iCalendar addresses** need no OAuth and work with Google, but they are read-only and act like
  passwords: anyone holding the address can read the calendar, so Switchboard stores them as secret references.
* **Calendar and contact synchronization** works with app passwords on iCloud and eligible Fastmail plans.
  The calendar connector can create events; the contacts connector only reads. Direct authenticated Google
  calendar and contact access needs the OAuth authorization framework, which those two connectors do not
  implement yet; use secret calendar feeds and exported contact files instead.
* **Invitation drafts are a handoff, not a calendar-write guarantee.** `calendar_propose_event` saves an
  invitation draft. Sending or importing it can add an event, depending on the receiving calendar and its
  invitation settings; it is not equivalent to an authenticated direct write.
* **A recurrence subset.** Common daily, weekly, monthly, and yearly rules expand correctly, including
  daylight-saving changes; rare parts (`BYSETPOS`, `BYWEEKNO`, `BYYEARDAY`, hourly rules) fall back to the first
  occurrence with a warning. Details: [calendar](./connectors/calendar.md#tradeoffs-and-limits).

## Money: bank data without an aggregator

* **Files and alerts, not scraping.** Logging in to your bank's website with your password from a script breaks
  most banks' terms, trips multi-factor prompts, and puts your credentials in a program. Switchboard never does it.
* **No live balances.** A statement file is as fresh as your last download. Alert emails fill the gap with pending
  entries that later match the posted transaction, so each purchase counts once.
* **Direct bank connections** are not implemented. Whether a bank offers an authorized free interface is
  institution-specific; manual exports do not become a live connection just because the parser accepts them.
* **Low-cost bridges exist** (SimpleFIN Bridge charges a small yearly fee), but they are still paid
  intermediaries. The United States Consumer Financial Protection Bureau's personal financial data rights
  rule remains under reconsideration, and its compliance dates are stayed. That does not establish a free
  working interface for your bank.
* **Privacy:** the ledger stores the last four digits and a salted hash, not the full account number. Original
  statement files are still retained in the import folders and may contain full numbers.
  Details: [finance](./connectors/finance.md#tradeoffs-and-limits).

## Money movement and checkout

There is no tool that pays, transfers, or buys, and the `spend` tier denies by default. Reasons:

* There is no implemented retailer checkout integration. Each would need an authorized interface, carefully
  scoped payment access, and its own error and refund handling; arbitrary browser automation is not a safe
  substitute.
* In [Visa's benchmark published November 5, 2026](https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22936.html),
  tested models passed 57.2% of shopping scenarios, and about 54% of failed runs still made a purchase
  decision. These are results for that benchmark, not a measured failure rate for Switchboard or a
  certification of real checkout execution.
* Approving each purchase in a terminal would be no faster than buying it yourself.

What you get instead: price watches that respect `robots.txt`, `Retry-After`, and a minimum interval per page,
then tell you (through `notify_self` and a rule) when an item reaches your target, with the link. Prices shown only
by JavaScript are invisible to a plain fetch. Details: [shopping](./connectors/shopping.md#why-there-is-no-buy-tool).

## Notifications, webhooks, and home

* **ntfy on the public server** has a free tier, but an unprotected topic name is effectively a password;
  Switchboard treats it as a secret. Self-hosting moves that trust to your own server. Telegram puts the bot
  token in the request path, so the connector avoids logging request addresses.
* **Provider webhooks need a public encrypted endpoint.** The receiver binds to `127.0.0.1`; exposing it means running a
  tunnel such as Tailscale Funnel or Cloudflare Tunnel, and every public endpoint attracts probing. Each hook is
  checked with the selected provider scheme. Static shared-token schemes do not prevent replay; not every
  hook carries a cryptographic signature. Polling avoids all
  of this and is the default. Details: [webhooks](./connectors/webhooks.md#push-or-poll).
* **Home Assistant** is reachable only for entities you expose, and locks, alarms, covers (including garage doors),
  and cameras are denied unless you list them explicitly.

## Telegram, LinkedIn, and WhatsApp

* **Telegram is a message bridge, not an assistant runtime.** Its poller needs no public listener and
  checks both sender and chat allowlists. An existing assistant must read the inbox and request a
  reply. The bridge does not log in as your personal account, download all chats, process every bot
  update type, or provide phone-based approval. One bot must have one update receiver.
* **LinkedIn exports are not a live inbox or feed.** Imports work with declared download formats; post
  drafts can stay local. Optional publishing requires your own registered application's authorized
  permissions and is gated by the exact actor, version, visibility, content, and draft identity. No
  scraping, session cookies, connection requests, organization posting, or inbox automation is implemented.
* **WhatsApp remains offline-only.** Export dates need an explicit interpretation and are not converted
  into supposedly proven global timestamps. The business platform is a different product, not a
  personal-account mirror. Its current terms restrict offering general-purpose assistants, with
  jurisdiction-specific exceptions. Current developer pricing states service-message charges resumed
  October 1, 2026; a 24-hour response window is not a universal zero-cost guarantee.
* **Local atomicity does not settle external delivery.** Inbox rows and event/cursor state commit
  together, as do export records and import-summary events. Replies and posts still cross a network
  boundary. An ambiguous attempt is quarantined rather than automatically retried; creating a new
  draft to retry the same content can still duplicate an external effect.

Details and primary sources: [Telegram](./connectors/telegram.md),
[LinkedIn](./connectors/linkedin.md), and [WhatsApp exports](./connectors/whatsapp.md). The code and synthetic
tests are authored, not live-provider verification.

## Architecture choices

| Choice                         | Benefit                                                            | Cost                                                                                                 |
| ------------------------------ | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Standard library only          | No third-party Python runtime dependency tree                      | Hand-written parsers; still maintain Python, system libraries, and development dependencies          |
| MCP over standard input/output | No listening port; the assistant starts and stops the server       | One process per assistant; calls are handled one at a time                                           |
| Dual-era MCP                   | Targets `2026-07-28`, `2025-11-25`, `2025-06-18`, and `2025-03-26` | Two paths to maintain; real-client conformance is untested                                           |
| One SQLite file                | Simple backups; write-ahead logging lets processes share it        | Events are not claimed across processes; a lock file allows one `switchboard run` per data directory |
| Approvals in the terminal      | Nothing new to expose; exact arguments are bound                   | You must be at a terminal; phone approvals are future work and need careful design                   |
| Hash-chained audit log         | Edits, interior deletions, and reordering can be detected          | Valid tail truncation needs an independent head hash; a complete rewrite is still possible           |
| Polling with long intervals    | Polite to providers; no public endpoint                            | Minutes of delay instead of seconds                                                                  |
| Python 3.11+ on Linux or macOS | `tomllib`, `zoneinfo`, and `fcntl` in the standard library         | Windows is untested                                                                                  |

## What this version cannot do

* Show live bank balances or reach banks that offer no downloads or alert emails.
* Provide native authenticated Google calendar or contact synchronization; only mail authorization is implemented.
* Buy anything, move money, or fill payment forms.
* Guarantee safety against prompt injection: it limits what an injected instruction can achieve, it does not
  make the assistant immune.
* Read prices that appear only after JavaScript runs.
* Claim events across several runner processes, or approve actions from a phone.
* Sandbox an assistant that has the same user's shell and file access.
* Guarantee exactly-once delivery of external effects across crashes, or automatically replay failed events safely.
* Create and renew Microsoft Graph subscriptions or Google Calendar watch channels. The receiver accepts
  notifications; it does not provision subscriptions, renew them, or automatically fetch the changed resource.
* Enforce a whole-request deadline or interrupt an already running tool on cancellation. Web requests have
  per-socket timeouts, not a bounded name-resolution and total-transfer deadline, and the tool server handles
  one request at a time.
* Validate arbitrary third-party schemas with a complete schema engine. The handwritten validator implements
  only the keywords used by the bundled tools, not general schema conformance under the 2020-12 draft.

Protocol references (checked 2026-11-11):
[current versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover), and the
[2025-06-18 change removing batches](https://modelcontextprotocol.io/specification/2025-06-18/changelog).

## Highest-value next work

1. **Run before connecting accounts.** Exercise the written unit and integration suites on a personal
   machine, then build a platform and Python-version matrix. Fake responses and type checks cannot prove
   protocol behavior against an actual provider.
2. **Durable per-action delivery.** Add an outbox with provider-supported idempotency keys, explicit retry
   policies, and operator-controlled recovery. Avoid promising exactly-once execution where a provider has
   no such guarantee.
3. **Provider lifecycle support.** Register and renew notification subscriptions, persist change cursors,
   and recover missed notifications before calling push integration complete.
4. **Stronger isolation.** Separate assistant execution from credentials and approvals, rather than relying
   on the same local user's command line as a human identity check.
5. **Explicit deletion and reproducibility.** Add component-specific retention and export controls, a
   repeatable dependency lock and test environment, restore tests, and an automated test pipeline. Choose a
   license and confirm ownership before publication.

## Testing status

All tests were written alongside the code and checked statically (Ruff linting and formatting, mypy type
checking of the package and the tests), but none were executed on the authoring machine. Runtime behavior,
platform compatibility, and real provider flows remain unverified. Run the offline suite on your own machine
before enabling accounts; then validate one provider at a time with the smallest permitted scope.
