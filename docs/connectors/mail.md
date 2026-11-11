# Mail connector

The mail connector lets your assistants search, read, and draft email in your own mailboxes over the
Internet Message Access Protocol (IMAP), and send email over the Simple Mail Transfer Protocol (SMTP)
once you approve. It signs in to your provider directly, with an app password or with OAuth 2.0
tokens (the authorization framework of Request for Comments (RFC) 6749) issued to your own client
registration. No paid aggregator sits in between.

* Reading is read-only on the server: nothing is marked as read, moved, flagged, or deleted.
* Drafting writes only to your Drafts mailbox, so you finish and send the draft yourself.
* Sending is off until you turn it on per account, and then it still asks for approval by default.
* Watched mailboxes produce `mail.received` events that rules and other connectors (finance,
  shopping) can react to.

Provider facts below were **checked 2026-11-11** against the linked official pages. Providers change
these pages; if something no longer matches, trust the provider and update your configuration.

## Tools

| Tool             | Tier    | Purpose                                                                                        |
| ---------------- | ------- | ---------------------------------------------------------------------------------------------- |
| `mail_accounts`  | `READ`  | List configured accounts: name, address, provider, sign-in method, sending, watched mailboxes. |
| `mail_mailboxes` | `READ`  | List one account's mailboxes with their special-use flags (`\Drafts`, `\Sent`, …).             |
| `mail_search`    | `READ`  | Search one mailbox (default `INBOX`); at most 50 newest-first summaries.                       |
| `mail_read`      | `READ`  | Read one message by unique identifier (UID): headers, capped text, attachment metadata.        |
| `mail_draft`     | `DRAFT` | Save a plain-text draft in the Drafts mailbox, optionally threaded as a reply. Never sends.    |
| `mail_send`      | `ACT`   | Send a plain-text message through SMTP. Asks for approval by default; needs `send_enabled`.    |

`mail_search` accepts `from`, `to`, `subject`, `text` (headers or body), `since` and `before`
(`YYYY-MM-DD`, compared with the date the server received the message; `before` is exclusive),
`unseen`, and `limit` (1–50, default 20). Results carry `uid`, `date`, `from`, `to`, `subject`,
`flags`, and `size`, plus `matched` (the total number of hits).

`mail_read` returns decoded headers, `text` capped at `max_body_chars` with a `truncated` flag,
`partial: true` when the message was larger than the 10 MiB fetch limit, attachment names, types, and
sizes (never their content), and `untrusted_content: true`.

`mail_draft` and `mail_send` take `account`, `to` (required), `cc`, `subject`, `body`, and optionally
`in_reply_to_uid` with `in_reply_to_mailbox` (default `INBOX`): the reply then gets `In-Reply-To` and
`References` headers from the original. `mail_send` also takes `bcc`. Addresses must be plain
(`name@example.com`, no display names), at most 100 per message. Blind carbon copy (Bcc) recipients
go only into the SMTP envelope, never into the message headers.

For approval, `mail_send` shows one line such as
`send 'Lunch on Friday?' from personal to sam@example.org, jordan@example.com`, and it declares every
To, carbon copy (Cc), and Bcc address as recipients so `allow_recipients` rules apply to all of them.

A server may accept a message for some recipients and refuse others. `mail_send` then returns
`status: "partially_sent"` instead of `"sent"`, with the accepted addresses in `recipients` and the
refused ones in `refused`; the audit log records both lists. When the server refuses every recipient,
the call fails and nothing is sent.

## Events

`mail.received` is emitted by the `mail.poll` job for each new message in a watched mailbox.

| Key               | Type             | Meaning                                                               |
| ----------------- | ---------------- | --------------------------------------------------------------------- |
| `account`         | string           | Account name from the configuration.                                  |
| `mailbox`         | string           | Watched mailbox name.                                                 |
| `uid`             | integer          | The message's UID in that mailbox.                                    |
| `message_id`      | string           | The `Message-ID` header (may be empty).                               |
| `from`            | string           | Decoded sender, for example `Alex Example <alex@example.com>`.        |
| `to`, `cc`        | list of strings  | Decoded recipients in the same form.                                  |
| `subject`         | string           | Decoded subject.                                                      |
| `date`            | string or null   | The `Date` header as ISO 8601 in Coordinated Universal Time.          |
| `text`            | string           | Plain text, at most 20,000 characters (or `max_body_chars` if lower). |
| `truncated`       | boolean          | Whether `text` was cut.                                               |
| `has_attachments` | boolean          | Whether any attachment was found.                                     |
| `attachments`     | list of objects  | `filename`, `content_type`, `size` for each attachment.               |

The dedupe key is `mail:<account>:<mailbox>:<uidvalidity>:<uid>`, so a retried poll cannot emit the
same message twice.

How polling decides what is new: every watched mailbox has a cursor in the key-value store
(namespace `mail`, key `<account>:<mailbox>`, value `{"uidvalidity": …, "last_uid": …}`). IMAP
promises that UIDs only grow while the mailbox's `UIDVALIDITY` value stays the same, so "UIDs above
`last_uid`" is exactly the new mail.

* The **first poll** records the current position (`UIDNEXT - 1`) and emits nothing, so enabling the
  connector never floods your rules with old mail.
* If **`UIDVALIDITY` changes** (the server rebuilt the mailbox), the cursor is reset the same way,
  without emitting.
* Each poll handles at most **50** new messages, oldest first; the rest wait for the next poll.
* One failing account or mailbox is logged and skipped; the others are still polled, and the job
  reports the failure afterwards.

## Services

| Name          | Interface                                                               |
| ------------- | ----------------------------------------------------------------------- |
| `mail.drafts` | `save_draft(message: EmailMessage, account: str \| None = None) -> str` |
| `mail.sender` | `send(message: EmailMessage, account: str \| None = None) -> str`       |

When `account` is `None` the first usable account in configuration order is used. Missing `From`,
`Date`, and `Message-ID` headers are filled in. `save_draft` returns `<account>:<mailbox>:<Message-ID>`;
`send` returns the Message-ID and sends to every To, Cc, and Bcc address.

Services are called by other connectors (for example the notify connector's email channel), not by
assistants, so the policy gate does not apply to them; the calling connector's own tools are gated.
`send` still refuses accounts without `send_enabled = true`. Failures raise `ToolError`.

The mail connector does not use any other connector's service and subscribes to no events.

## Configuration

Put accounts under `[connectors.mail.accounts.<name>]`; names may use letters, digits, `-`, and `_`.
A ready-to-merge example with policy rules is in [docs/config/mail.toml](../config/mail.toml).

| Key                      | Default                           | Meaning                                                                          |
| ------------------------ | --------------------------------- | -------------------------------------------------------------------------------- |
| `poll_interval_seconds`  | `300`                             | How often watched mailboxes are polled (60 to 86,400).                           |
| `max_body_chars`         | `20000`                           | Cap for `mail_read` text (500 to 200,000). Events never exceed 20,000.           |
| `provider`               | required                          | `gmail`, `outlook`, `icloud`, `fastmail`, or `custom`.                           |
| `address`                | required                          | Your address; also the `From` of drafts and sent mail.                           |
| `username`               | `address`                         | Sign-in name for IMAP (and SMTP unless `smtp_username` is set).                  |
| `smtp_username`          | `username`                        | Sign-in name for SMTP when it differs (iCloud).                                  |
| `auth`                   | `"password"`                      | `"password"` (an app password) or `"oauth2"`.                                    |
| `password`               | —                                 | Secret reference to the app password, such as `cmd:pass show mail/app`.          |
| `client_id`              | —                                 | Your OAuth client identifier (`auth = "oauth2"`).                                |
| `client_secret`          | none                              | Secret reference to the client secret, if your Google client has one.            |
| `tenant`                 | `"consumers"`                     | Microsoft sign-in audience; `consumers` means personal accounts.                 |
| `imap_host`, `imap_port` | provider preset, `993`            | IMAP server; always implicit Transport Layer Security (TLS).                     |
| `smtp_host`, `smtp_port` | provider preset                   | SMTP submission server.                                                          |
| `smtp_security`          | `"ssl"` on 465, else `"starttls"` | Implicit TLS (`"ssl"`) or an upgrade with the STARTTLS command.                  |
| `watch`                  | `[]`                              | Mailboxes to poll for `mail.received`; empty means no polling.                   |
| `send_enabled`           | `false`                           | Allow `mail_send` and `mail.sender` for this account.                            |
| `drafts_mailbox`         | detected                          | Where drafts go; otherwise the special-use `\Drafts` mailbox, then common names. |

Every connection uses TLS with certificate checks against your system's
trust store; there is no plaintext mode. Secrets are references (`env:`, `file:`, `cmd:`) resolved
only when a connection is made.

Check a new account with:

```bash
switchboard doctor
switchboard mail check personal
```

`doctor` reports configuration problems, secret references that do not resolve, and missing OAuth
sign-ins, without connecting anywhere and without printing secret values. `mail check` signs in over
IMAP and prints how many mailboxes it found and which one it will use for drafts.

## Provider setup

### Gmail

Servers ([Google](https://developers.google.com/workspace/gmail/imap/imap-smtp)): IMAP
`imap.gmail.com:993` with implicit TLS; SMTP `smtp.gmail.com:465` with implicit TLS, or `:587` with
STARTTLS. Since January 2025, IMAP is always on for personal accounts
([Google](https://support.google.com/mail/answer/7126229)).

**Recommended for a free personal setup: an app password.**

1. Turn on 2-Step Verification for your Google Account. App passwords require it, and Google notes
   they may be unavailable when 2-Step Verification uses only security keys, with Advanced
   Protection, or for work and school accounts ([Google](https://support.google.com/accounts/answer/185833)).
2. Create an app password at <https://myaccount.google.com/apppasswords> and store it in your secret
   store. Google revokes app passwords when you change your Google password.
3. Configure `provider = "gmail"`, `auth = "password"`, and `password = "cmd:…"`.

Google labels app passwords "not recommended" and prefers "Sign in with Google". Switchboard supports
that too (`auth = "oauth2"`), but read the [tradeoffs](#app-passwords-versus-oauth) first.

**OAuth sign-in.** Create a project in the Google Cloud console, configure the OAuth consent screen
for an external audience with yourself as a test user, and create an OAuth client of type **Desktop
app** ([Google](https://developers.google.com/identity/protocols/oauth2/native-app)). Set
`client_id` and, if the console gave you one, `client_secret` as a secret reference. Then run
`switchboard mail oauth-login <account>`: it opens your browser, listens once on
`http://127.0.0.1:<random port>` for the redirect, and uses Proof Key for Code Exchange (PKCE) with
the S256 method. The scope is `https://mail.google.com/`, the only scope that covers IMAP and SMTP
([Google](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol)). IMAP and SMTP then
sign in with the token through the XOAUTH2 mechanism of the Simple Authentication and Security Layer
(SASL).

Gmail copies mail sent through its SMTP server into Sent automatically
([Google](https://support.google.com/mail/answer/78892)). Personal accounts may send up to 500
messages a day through mail clients, with at most 100 recipients per message
([Google](https://developers.google.com/workspace/gmail/imap/imap-smtp)).

### Outlook.com (personal Microsoft accounts)

Servers ([Microsoft](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com)):
IMAP `outlook.office365.com:993` with implicit TLS; SMTP `smtp-mail.outlook.com:587` with STARTTLS.
The page states that Outlook.com requires OAuth 2.0 ("Modern Auth") and that IMAP access is off by
default: turn it on in Outlook.com under **Settings > Mail > Forwarding and IMAP > Let devices and
apps use IMAP**. Microsoft ended basic authentication (signing in by sending a user name and
password) for Outlook accounts on September 16, 2024
([Microsoft](https://support.microsoft.com/en-us/office/modern-authentication-methods-now-needed-to-continue-syncing-outlook-email-in-non-microsoft-email-apps-c5d65390-9676-4763-b41f-d7986499a90d)),
so this connector accepts only `auth = "oauth2"` for `outlook`.

**Register your own free app** in the Microsoft Entra admin center:

1. **App registrations > New registration**; under **Supported account types** choose **Personal
   accounts only** ([Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)).
   Registering needs a Microsoft Entra tenant; Microsoft's
   [tenant quickstart](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-create-new-tenant)
   lists an Azure account with an active subscription as the prerequisite and links to a free
   sign-up; check the current terms.
2. Copy the **Application (client) ID** into `client_id`. No client secret is needed.
3. Under **Authentication > Advanced settings**, set **Allow public client flows** to **Yes**; the
   device flow needs it
   ([Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-registration)).

Then run `switchboard mail oauth-login <account>`. It uses the device authorization grant
(RFC 8628): it prints a short code and a web page; you sign in there (on any device) within 15
minutes, and the command stores the tokens. The requested scopes are
`https://outlook.office.com/IMAP.AccessAsUser.All`, `https://outlook.office.com/SMTP.Send`, and
`offline_access` for a refresh token
([Microsoft](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)).
`tenant` defaults to `consumers`; `common` also works for personal accounts
([Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)).

### iCloud Mail

Servers ([Apple](https://support.apple.com/en-us/102525)): IMAP `imap.mail.me.com:993` with implicit
TLS; SMTP `smtp.mail.me.com:587`, used here with STARTTLS. Apple says the IMAP user name is usually
the part of your iCloud address before the `@` (try the full address if that fails), while SMTP needs
the full address. Switchboard uses the full address for both by default; if IMAP sign-in fails, set
`username = "<name part>"` and `smtp_username = "<full address>"`.

Create an app-specific password at <https://account.apple.com> under **Sign-In and Security >
App-Specific Passwords**. It needs two-factor authentication on your Apple Account; you can have up to
25, and changing your Apple Account password revokes them all
([Apple](https://support.apple.com/en-us/102654)).

### Fastmail

Servers ([Fastmail](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports)):
IMAP `imap.fastmail.com:993` with implicit TLS; SMTP `smtp.fastmail.com:465` with implicit TLS (or
`:587` with STARTTLS). The user name is your full Fastmail address. An app password is required; your
regular password does not work, and Basic plans do not include IMAP or SMTP. Create one under
**Settings > Privacy & Security > Connected apps & API tokens > Manage app passwords and access >
New app password**
([Fastmail](https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords)).

### Other servers

Use `provider = "custom"` with `imap_host` (implicit TLS, port 993 unless set) and, to send,
`smtp_host`, `smtp_port`, and optionally `smtp_security`. Only app passwords are supported for custom
servers in this version.

## Design decisions and tradeoffs

### App passwords versus OAuth

An app password is the durable free path for Gmail, iCloud, and Fastmail: it does not expire on a
timer, and it is revoked when you change the account password or remove it yourself. It does grant
full mailbox access, so keep it in a password manager and reference it with `cmd:`.

Google OAuth avoids a long-lived password, but Gmail's scope is restricted:

* While the consent screen's publishing status is **Testing**, Google issues refresh tokens that
  expire after **7 days** for any scope beyond basic profile information, so you would sign in again
  every week ([Google](https://developers.google.com/identity/protocols/oauth2#expiration)). Refresh
  tokens with Gmail scopes also stop working when you change your password, or after six months
  without use.
* `https://mail.google.com/` is a **restricted** scope
  ([Google](https://developers.google.com/workspace/gmail/api/auth/scopes)). Publishing an app that
  uses it needs Google's verification, and apps that access restricted data through a server need an
  annual third-party security assessment
  ([Google](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)).
  The same page exempts personal use (you are the only user, or a few people you know), in which case
  you click through Google's unverified-app warning; unverified apps are capped at 100 users
  ([Google](https://support.google.com/cloud/answer/7454865)).

Microsoft offers no app passwords for Outlook.com any more, so OAuth with your own app registration is
the only path there. Microsoft gives refresh tokens for this kind of app no specified lifetime, and a new
refresh token replaces the old one when Microsoft returns one
([Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow#refresh-the-access-token)).

Tokens live in `<data_dir>/mail/oauth/<account>.json` (directory mode 700, file mode 600, written
atomically, symbolic links refused). They are not encrypted, so protect the data directory as you
would the mailbox. Access tokens are refreshed two minutes before they expire.

### Polling versus IMAP IDLE

The IMAP IDLE command (RFC 2177) pushes new-mail notices over a connection that stays open. This
connector polls instead: the core runner is a simple interval scheduler, an open connection per
mailbox needs its own thread and reconnect logic, and Gmail ends IMAP sessions that signed in with
OAuth after about an hour anyway
([Google](https://developers.google.com/workspace/gmail/imap/imap-smtp)). The cost is latency: new
mail is noticed within `poll_interval_seconds` (5 minutes by default). Each poll opens one connection
per account, opens each watched mailbox read-only, and returns early when nothing changed.

### Why sending is off by default

Email is the easiest way for a mistake, or a prompt injected into a message, to reach other people.
Sending therefore needs three things: `send_enabled = true` on the account, a passing policy rule
(`ACT` tools ask by default; `allow_recipients`, `max_per_hour`, and the kill switch apply), and your
approval of the exact stored arguments. `mail_draft` is the everyday tool: the assistant writes, you
read and send from your own client.

### Read-only reading

Mailboxes are opened with `EXAMINE` and messages fetched with `BODY.PEEK`, so reading never sets the
seen flag. The only write the connector performs is an `APPEND` of a draft with the `\Draft` flag.
There are no tools to move, flag, or delete mail.

### Attachments

Version 0.1 neither returns attachment content nor sends attachments. Reading reports file names,
types, and sizes only, because attachment content is large, often binary, and a common carrier of
malicious payloads. Sending attachments would need file access, which the connector guide restricts to
configured directories.

### Search terms with accents or other scripts

IMAP quoted strings are 7-bit (RFC 3501), and Python's `imaplib` attaches at most one literal to a
command. Search terms made only of 7-bit characters are sent as escaped quoted strings; **one** term with other
characters (`subject = "Café"`) is sent last as a UTF-8 literal with `CHARSET UTF-8`. A search with two
such terms is refused with a clear message rather than silently mangled. Mailbox names are converted
to and from IMAP's modified UTF-7 encoding, so you see and type `Entwürfe`, not `Entw&APw-rfe`.

### Other limits

* Messages are fetched up to their first 10 MiB; larger messages are marked `partial`, and attachments
  past that point are not listed.
* The connector does not save a copy of sent mail in Sent. Gmail does that by itself; check your
  provider's behavior.
* Recipients must be plain addresses without display names, so policy patterns match exactly.
* Text is plain only. HyperText Markup Language (HTML) mail is converted to text for reading; drafts
  and sent messages are plain text.
* Two processes refreshing the same OAuth token at once both succeed; the last one to write wins.

## Security notes

* Secrets are never logged, returned, or included in errors; `repr` of credential objects hides them.
* Message content is untrusted data. Results set `untrusted_content: true`; HTML is reduced to text
  without rendering, links are never followed, scripts and styles are dropped, and control characters
  (terminal escape sequences) are removed from headers and text.
* Every IMAP argument is quoted, and carriage returns, line feeds, and NUL characters are refused in
  search terms, mailbox names, and subjects, which prevents command and header injection.
* OAuth sign-in uses PKCE and a random `state` compared in constant time; the loopback listener binds
  only to 127.0.0.1, serves one request, and never logs the request line, which carries the code.
* Sign-in failures and refresh errors name the problem (for example an expired refresh token) and
  tell you to run `switchboard mail oauth-login` again; they never include tokens.

## Sources

All checked 2026-11-11.

* Google: [Gmail IMAP and SMTP servers](https://developers.google.com/workspace/gmail/imap/imap-smtp),
  [XOAUTH2 protocol](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol),
  [OAuth for installed apps](https://developers.google.com/identity/protocols/oauth2/native-app),
  [refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration),
  [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
  [restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
  [unverified apps](https://support.google.com/cloud/answer/7454865),
  [app passwords](https://support.google.com/accounts/answer/185833),
  [Gmail in other clients](https://support.google.com/mail/answer/7126229),
  [IMAP client settings](https://support.google.com/mail/answer/78892).
* Microsoft: [Outlook.com server settings](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com),
  [basic authentication retirement](https://support.microsoft.com/en-us/office/modern-authentication-methods-now-needed-to-continue-syncing-outlook-email-in-non-microsoft-email-apps-c5d65390-9676-4763-b41f-d7986499a90d),
  [OAuth for IMAP and SMTP](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth),
  [device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code),
  [refreshing tokens](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow#refresh-the-access-token),
  [registering an app](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app),
  [public client flows](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-registration),
  [creating a tenant](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-create-new-tenant).
* Apple: [iCloud Mail server settings](https://support.apple.com/en-us/102525),
  [app-specific passwords](https://support.apple.com/en-us/102654).
* Fastmail: [server names and ports](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports),
  [app passwords](https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords).
* Standards: [RFC 3501](https://www.rfc-editor.org/rfc/rfc3501) (IMAP; modified UTF-7 in section
  5.1.3, the `n:*` UID range rule in section 6.4.8), [RFC 6154](https://www.rfc-editor.org/rfc/rfc6154)
  (special-use mailboxes), [RFC 4315](https://www.rfc-editor.org/rfc/rfc4315) (`APPENDUID`),
  [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628) (device authorization grant),
  [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) (PKCE), [RFC 8314](https://www.rfc-editor.org/rfc/rfc8314)
  (implicit TLS for mail), [RFC 5322](https://www.rfc-editor.org/rfc/rfc5322) (message format and
  threading headers).
