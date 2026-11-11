# Webhooks connector

The webhooks connector receives Hypertext Transfer Protocol (HTTP) requests from services that push
changes, such as GitHub, Microsoft Graph, Google Calendar, or your own scripts. It verifies each
delivery and turns the verified ones into `webhook.received` events for connector subscribers and
your automation rules. Everything else is refused, and refusals are written to the audit log without
the request body.

## Running it

```bash
switchboard webhooks serve                    # listens on 127.0.0.1:8787
switchboard webhooks serve --port 9000        # another port, still loopback only
switchboard run                               # a separate process: polls jobs and processes events
```

The receiver accepts `POST /hooks/<name>` and nothing else. It resolves every hook's secret when it
starts and refuses to start if one does not resolve. Restart it after you rotate a secret. Events
wait in the database until `switchboard run` processes them, so keep both running.

## Tools

| Name              | Tier | Purpose                                                                         |
| ----------------- | ---- | ------------------------------------------------------------------------------- |
| `webhooks_recent` | read | The last 1–50 verified deliveries: hook, time received, and size, never bodies  |

## Events

`webhook.received`, emitted once per verified delivery, with this payload:

| Key            | Meaning                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------- |
| `hook`         | The hook name from `/hooks/<name>`                                                           |
| `received_at`  | Coordinated Universal Time (UTC) in ISO 8601 format, for example `2027-01-15T08:00:00+00:00` |
| `content_type` | The request's `Content-Type`, up to 200 characters                                           |
| `headers`      | A safe subset of the request headers (see the security notes)                                |
| `size`         | Body size in bytes                                                                           |
| `json`         | The decoded body, when the body is JavaScript Object Notation (JSON)                         |
| `text`         | Otherwise the body as text, capped at 20,000 characters                                      |

For Microsoft Graph, every `clientState` key is removed from `json` because it holds the shared
secret. A rule can match these events like any other:

```toml
[[rules]]
name = "failed nightly build"
on = "webhook.received"
[rules.match]
"payload.hook" = "ci"
"payload.json.status" = "failed"
[[rules.do]]
tool = "notify_self"
args = { title = "Nightly build failed", message = "Job {payload.json.job} failed" }
```

The connector subscribes to no events and provides no services.

## Responses

| Situation                                                          | Status | Body                         |
| ------------------------------------------------------------------ | ------ | ---------------------------- |
| Verified and queued as an event                                    | 202    | `accepted`                   |
| Verified, but already received (a retry or redelivery)             | 202    | `duplicate`                  |
| Microsoft Graph validation request                                 | 200    | the decoded token            |
| Google Calendar `sync` message                                     | 200    | empty                        |
| Missing or wrong signature, token, or `clientState`; a replay      | 401    | `verification failed`        |
| Unknown hook or any other path                                     | 404    | `unknown hook`               |
| A method other than `POST`                                         | 405    | `only POST is accepted`      |
| A chunked body (no `Content-Length`)                               | 411    | a short explanation          |
| A body larger than `max_body_bytes`, refused before it is read     | 413    | `body too large`             |
| The hook's secret reference does not resolve                       | 503    | `receiver is not configured` |

Every response the receiver writes is short plain text with `X-Content-Type-Options: nosniff`, and
the connection closes after each request.

## Verification schemes

Set `verify` in `[connectors.webhooks.hooks.<name>]`. Every scheme takes a `secret` reference
(`env:`, `file:`, or `cmd:`), and every comparison with a secret runs in constant time
(`hmac.compare_digest`).

### `hmac-sha256`: your own senders

The sender signs each request with a keyed-hash message authentication code (HMAC) using Secure Hash
Algorithm 256 (SHA-256):

```text
X-Switchboard-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">
```

The timestamp must be within `tolerance_seconds` (default 300) of the receiver's clock, in either
direction. A signature that was already accepted is refused until it expires, and that record lives
in the database (table `webhooks_seen`), so restarting the receiver does not reopen the window.
A sender that retries must sign again with a fresh timestamp. A Python sender:

```python
import hashlib
import hmac
import time


def switchboard_signature(secret: bytes, body: bytes) -> str:
    timestamp = int(time.time())
    digest = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
```

### `github` (checked 2026-11-11)

From GitHub's guides on [validating deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries),
[delivery headers](https://docs.github.com/en/webhooks/webhook-events-and-payloads#delivery-headers),
and [best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks):

* `X-Hub-Signature-256` is `sha256=` followed by the hex HMAC-SHA256 of the raw body, keyed with the
  webhook secret. GitHub says to compare it in constant time. The unit tests use GitHub's published
  test values.
* GitHub expects a 2XX response within 10 seconds; the receiver answers as soon as the event is
  queued.
* A redelivery keeps the same `X-GitHub-Delivery` identifier. GitHub signs only the body, so a
  captured delivery could be resent under a new identifier; the receiver therefore treats a repeated
  signature as a duplicate instead of a new event.
* GitHub can send JSON or form-encoded bodies. Choose `application/json` when you create the webhook;
  a form-encoded body arrives as `text`.
* GitHub sends payloads up to 25 megabytes. Pushes with many commits can exceed the default
  `max_body_bytes` and get 413; raise the limit for that hook's sender if you need them.

### `token-header`: senders limited to a static header

`header = "X-Your-Header"` names the header whose value must equal the secret. For a sender that can
only set `Authorization: Bearer <token>`, use `header = "Authorization"` and store the whole value,
`Bearer <token>`, as the secret. A static header cannot stop replays: anyone who sees one request can
send it again. Use it only through a tunnel with Hypertext Transfer Protocol Secure (HTTPS).

### `msgraph`: Microsoft Graph change notifications (checked 2026-11-11)

From Microsoft Learn on [webhook delivery](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)
and [lifecycle notifications](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events):

* When you create a subscription, Graph sends `POST <notificationUrl>?validationToken=<token>`.
  Within 10 seconds, the endpoint must answer `200 OK` with content type `text/plain` and, as the
  body, the token decoded from its Uniform Resource Locator (URL) encoding. The receiver does exactly
  that, without creating an event.
* Each change notification in `value` carries the `clientState` you chose when you created the
  subscription; Microsoft says to treat a mismatch as a possibly forged notification. The receiver
  refuses the whole request unless **every** item matches. Lifecycle notifications
  (`reauthorizationRequired`, `subscriptionRemoved`, `missed`) carry `clientState` too, so the same
  hook can serve as the `lifecycleNotificationUrl`.
* Graph counts a notification as delivered when it gets a 2XX response within 3 seconds and
  recommends `202 Accepted` after queuing. Otherwise it retries for up to 4 hours, and notifications
  from an endpoint that keeps responding slowly can be dropped for good.
* The notification address must be public and use HTTPS. Retries are deduplicated only when every item
  carries a notification identifier, using that identifier and its subscription. Items without identifiers,
  including lifecycle notifications, are not deduplicated by body: identical bodies can be separate events.

This connector does not create or renew subscriptions. For
[notifications with resource data](https://learn.microsoft.com/en-us/graph/change-notifications-with-resource-data),
it does not check the JSON Web Tokens in `validationTokens` or decrypt `encryptedContent`. Use basic
notifications, which carry only identifiers, and fetch the changed item with your own credentials.

### `google-calendar`: Google Calendar push channels (checked 2026-11-11)

From Google's [push notifications guide](https://developers.google.com/workspace/calendar/api/guides/push):

* A channel is created with a `watch` request whose `address` must use HTTPS with a valid certificate;
  self-signed certificates are refused.
* The optional `token` (up to 256 characters) comes back in `X-Goog-Channel-Token` on every message;
  Google suggests it for verifying that a notification is not spoofed. Set it to the same random value
  your `secret` reference resolves to.
* The first message has `X-Goog-Resource-State: sync` (and message number 1). The receiver answers it
  with 200 and no event. Later messages say `exists` or `not_exists`.
* Messages have no body; they say only that something changed. Fetch the changes yourself.
* Google accepts 200, 201, 202, 204, or 102 as success. Channels expire; create a new one before
  that.
* Deliveries use the `APIs-Google` user agent and respect `robots.txt`, so do not block `/hooks/` in
  a `robots.txt` on the same host name.

Repeated messages (the same channel identifier and message number) are recorded as duplicates.

## Exposure (checked 2026-11-11)

The receiver binds `127.0.0.1` by default, so nothing outside the machine can reach it. Services such
as GitHub, Graph, and Google need a public HTTPS address. Use a tunnel you control instead of opening a
port on your router:

* [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) is available on all Tailscale plans,
  including the free Personal plan ([pricing](https://tailscale.com/pricing)); it is in beta.
  `tailscale funnel 8787` publishes `https://<device>.<tailnet>.ts.net` and forwards to
  `http://127.0.0.1:8787`. Funnel listens only on ports 443, 8443, and 10000, provisions the
  certificate itself, and its relays do not decrypt the traffic. Bandwidth limits apply and cannot be
  configured, and new public Domain Name System (DNS) records can take up to 10 minutes.
* [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/) is available on all Cloudflare
  plans, including the free plan. It needs a Cloudflare account and
  [a domain on Cloudflare](https://developers.cloudflare.com/tunnel/get-started/), and `cloudflared`
  makes only outbound connections. Traffic passes through Cloudflare's network, where its caching,
  firewall, and bot management apply; Cloudflare decrypts the traffic to apply them, so it can read
  the bodies.

Behind either tunnel, every request reaches the receiver from `127.0.0.1`, so address allowlists do
not work here; verification is the only gate. If you bind another address with `--host`, the
receiver warns you, and it never terminates encryption itself.

### Push or poll?

A public endpoint invites probing. Scanners will find it, and each refused request adds an audit
record. Push hooks also need upkeep: Graph subscriptions and Google channels expire and must be
renewed, and a notification is only a doorbell, after which you still fetch the change. Polling (for
example, the calendar connector's scheduled refresh) needs no public endpoint at all. Push is worth it
when you need changes within seconds, or when a sender (such as GitHub) only pushes.

## Settings

See [docs/config/webhooks.toml](../config/webhooks.toml). `max_body_bytes` (default 262,144; from 256
to 4,194,304) applies to every hook. `tolerance_seconds` (default 300; from 30 to 3,600) applies to
`hmac-sha256` hooks.

## Limits and tradeoffs

* JSON bodies are stored whole in the event, so an event can be as large as `max_body_bytes`. That is
  larger than the connector guide's usual payload of about 20 kilobytes; lower `max_body_bytes` if
  your senders send less.
* Secrets are resolved once at startup and kept in memory until the receiver stops.
* At most 32 connections are served at once; extra ones are closed immediately. Each connection has
  a 30-second total deadline, not just a per-read timeout, so a client that trickles one byte at a
  time cannot hold a thread.
* One request per connection. Chunked bodies are refused with 411. The receiver does not answer
  `Expect: 100-continue`, so `curl` waits about a second before sending bodies over 1 kilobyte; pass
  `-H 'Expect:'` when testing.
* After refusing a body, the receiver reads and discards up to 1 mebibyte of it so the client sees
  the status code instead of a reset connection.
* Events are processed later by `switchboard run`, not inside the request.

## Security notes

* Secrets are never read from query strings, and the access log records only the method, path, and
  status, never a query string.
* A refused request gets a bare 401; the reason (for example, `signature mismatch`) goes only to the
  audit log, together with the hook name and the body size, never the body or headers.
* Event headers exclude any header whose name mentions authorization, cookies, tokens, secrets,
  signatures, passwords, application programming interface (API) keys, sessions, or forwarded client
  addresses, plus the configured `token-header` header and connection-level headers. At most 40
  headers are kept, each cut to 300 characters.
* Webhook bodies are untrusted data. Rules may match on them, but nothing executes them, and the
  assistant is instructed to treat webhook payloads as data, never as instructions.
