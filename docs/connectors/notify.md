# Notify connector

The notify connector sends short notes to **you**: a push notification on your phone through ntfy, a message in your own Telegram chat, or an email to your own inbox.
It provides:

* the `notify_self` tool, which assistants and automation rules call; and
* the `notify` service, which other connectors call (`send(title, message, *, priority="default", url=None) -> list[str]`).

Every destination comes from your configuration.
Nothing an assistant passes can add a recipient.

## Tools

| Name          | Tier  | Purpose                                                                               |
| ------------- | ----- | ------------------------------------------------------------------------------------- |
| `notify_self` | draft | Send a title, a message, a priority, and an optional link to your configured channels |

Arguments: `title` (1–120 characters), `message` (1–2,000 characters), `priority` (`min`, `low`, `default`, `high`, or `urgent`), `url` (optional, must start with `https://`, no spaces), and `channels` (optional list of channel names; omit it to use every channel).
The result lists the channels that accepted the note and why the others failed, for example `{"sent": ["chat"], "failed": {"phone": "ntfy answered HTTP 403"}}`.
The call fails only when no channel accepted it.

A channel that may have delivered the note anyway is listed under `uncertain` instead, and the result then carries `"effect_unknown": true`: a connection lost while sending, an HTTP 408 or 5xx reply, a Telegram reply without `ok: true`, or an email the mail connector could not confirm.
The call's action is then recorded as `uncertain`, and an identical note is refused until you check the device and run `switchboard actions resolve`.
When no channel accepted the note and at least one is uncertain, the call reports an error with an uncertain action.
Channels after the call's deadline are not attempted and are listed under `failed`.

### Why `notify_self` is a draft-tier tool

Switchboard's tiers say that draft tools prepare something you finish yourself, while act tools send, create, or change things outside Switchboard.
`notify_self` has no recipient argument: it can reach only the channels you listed, and every one of them ends with you.
The worst a confused or manipulated assistant can do is pester you, so the suggested policy caps it at 30 calls per hour instead of asking you to approve each alert.
Asking would defeat the purpose of an alert, and automation rules rely on this tool.

If you point a channel at anything shared (a group chat, a public ntfy topic someone else reads, a mailing list), that reasoning no longer holds.
Set `decision = "ask"` for the tool in that case.

## Events and services

* Emits no events and subscribes to none.
* Provides the `notify` service.
  One failing channel never stops the others; `send` returns the names of the channels that accepted the note and logs the reasons for the rest.
* Uses the `mail.sender` service from the mail connector for `email` channels, looked up each time a note is sent.
  Without the mail connector, email channels fail with a clear message.

Input from other connectors is normalized rather than refused: titles become one line of at most 120 characters, messages are capped at 4,000 characters, an unknown priority becomes `default`, and a link that is not a plain `https://` address is dropped.

## Setup

Add channels under `[connectors.notify.channels.<name>]`; a complete example is in [docs/config/notify.toml](../config/notify.toml).
Secrets are always references (`env:`, `file:`,
or `cmd:`), never values.
Run `switchboard doctor` afterwards: it resolves every reference and checks its shape without printing it.

### ntfy (checked 2026-11-11)

Facts from the [ntfy publishing guide](https://docs.ntfy.sh/publish/):

* Publishing is a `POST` (or `PUT`) to `https://<server>/<topic>` with the message as the body.
* Without an account, **"the topic is essentially a password"**: anyone who knows the topic can read and send notifications.
  Switchboard therefore stores the topic as a secret reference.
  Use a long random topic, or an access token on a server with access control.
* The title, priority, tags, and click action travel in the `Title`, `Priority`, `Tags`, and `Click` headers (aliases of `X-Title`, `X-Priority`, `X-Tags`, and `X-Click`).
* Priorities are `min` (1), `low` (2), `default` (3), `high` (4), and `max`/`urgent` (5).
* Access tokens are sent as `Authorization: Bearer <token>`.
  Set `token` to a reference to one.
* Messages over 4,096 bytes become file attachments, so Switchboard trims the body to fit (ending with "…").
  Titles are limited to 1 kilobyte and tags to 512 bytes in total.
* On the public `ntfy.sh` server, the daily message limit is 250, and requests come from a bucket of 60 that refills at one request every 5 seconds.

Two details come from the ntfy server source rather than the guide.
Topic names must match `^[-_A-Za-z0-9]{1,64}$` ([server.go](https://github.com/binwiederhier/ntfy/blob/main/server/server.go)), and Switchboard refuses any other topic so a stray `/` cannot change the request path.
The server decodes Multipurpose Internet Mail Extensions (MIME) encoded words in headers ([util.go](https://github.com/binwiederhier/ntfy/blob/main/server/util.go)), so titles with accents or emoji are sent as `=?UTF-8?B?...?=` words from Request for Comments (RFC) 2047.

A server on your own network may use plain `http://` only with `allow_http = true` on that channel.

### Telegram (checked 2026-11-11)

Facts from the reference for Telegram's bot application programming interface, the [Bot API](https://core.telegram.org/bots/api).
That site was unreachable from the network where this connector was written, so the page was read from the [Internet Archive snapshot of 2026-11-05](https://web.archive.org/web/20261105012614/https://core.telegram.org/bots/api):

* Every request goes to `https://api.telegram.org/bot<token>/METHOD_NAME`, and JavaScript Object Notation (JSON) bodies are accepted.
  **The bot token is part of the address**, so Switchboard never logs, returns, or raises a request address for this channel, and it replaces the token with `[redacted]` if Telegram's error text ever repeats it.
* `sendMessage` takes `chat_id` (a number, or `@username` for a channel) and `text` of 1–4,096 characters.
  Switchboard sends plain text (no `parse_mode`), so nothing in a note is read as markup.
* `disable_notification` "sends the message silently"; Switchboard sets it for `min` and `low`.
* `link_preview_options` with `is_disabled` turns off link previews; Switchboard always disables them, so Telegram does not fetch the links in your notes.
* Replies carry `ok`, and failures add a human-readable `description`, which Switchboard reports.

To set up, create a bot and copy its token as Telegram's bot documentation describes, store the token where a secret reference can reach it, and send your bot any message.
Then run the following command yourself; `getUpdates` lists incoming messages, and `message.chat.id` in its output is your `chat_id`:

```bash
curl --silent "https://api.telegram.org/bot$(pass show switchboard/telegram-bot)/getUpdates"
```

Do not paste that command, or its output, anywhere: the address contains the token.

### Email

An `email` channel needs the mail connector, which provides `mail.sender`.
List only your own addresses in `to` (one to ten).
`account` picks one of the mail connector's accounts; leave it out to use that connector's default.
The note's title becomes the subject, and the link (if any) is appended to the body.
The mail connector is expected to fill in the sender address for the account.

## Suggested policy

```toml
[policy.tools.notify_self]
max_per_hour = 30
```

## Limits and tradeoffs

* **Delivery is best effort.**
  Switchboard does not retry.
  A failed channel is reported in the tool result and the log; other channels still receive the note.
  A channel whose outcome is unknown is never resent automatically.
  The `notify` service that other connectors use returns only the channels that accepted the note and logs the uncertain ones.
* **Public ntfy is convenient but shared.**
  Anyone who learns the topic reads your notifications.
  A self-hosted server, or ntfy access control with a token, removes that risk at the cost of running or configuring it.
* **Telegram stores your messages on Telegram's servers.**
  Keep secrets, codes, and account numbers out of notes regardless of channel.
* **Email depends on the mail connector** and is slower than push, but it leaves a searchable record.

## Security notes

* Treat links in notifications like links in email.
  An assistant chooses the `url`, so a manipulated assistant could send you a misleading link; the tool only guarantees that it starts with `https://` and has no spaces or control characters.
* Notes can carry text from untrusted sources (for example, a rule that quotes an email subject).
  Channels send plain text, and titles are reduced to one line so they cannot inject headers.
* Error messages, tool results, audit records, and logs never contain the ntfy topic, an ntfy token,
  the Telegram bot token, or a request address.
  Unexpected errors are reported by type only.
* To revoke access, delete the ntfy topic or token, and revoke the Telegram token as Telegram's bot documentation describes; then update the secret your reference points to.
