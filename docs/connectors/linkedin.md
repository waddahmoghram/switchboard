# LinkedIn: own exports and optional own-profile text posts

This connector reads your downloaded connection and message snapshots, saves immutable local post drafts,
and optionally publishes a text-only post to your own personal profile.
Publication is disabled by default.
There is no paid broker, scraping, browser cookie access, live inbox, feed reader, message sending, connection request, organization posting, media upload, post editing, deletion, or automatic retry.

An export is an editable historical file, not proof of sender identity, account ownership, current connection status, or complete conversation history.
Imported instructions and links remain untrusted data.
No imported message triggers a live-message event, and nothing follows an exported address or downloads an attachment.

## Local setup on your own machine

The following are instructions for your own machine, not a record of commands executed on the development host.
Merge the [complete configuration example](../config/linkedin.toml) into your private configuration.
Append `linkedin` to the existing `[switchboard].connectors` list without removing your other connectors.

Request your own data through LinkedIn's **Settings & Privacy > Data Privacy > Download your data**, as described in the [official account-download instructions][account-download].
Choose connections, messages, or a larger archive.
Do not use another person's export.
Email addresses may be absent because connections control their export visibility.

Use a dedicated directory you own with mode `700`.
Files must be owned regular files, not symbolic links,
and must not be writable by your group or other users; mode `600` is recommended.
Directory links and file links are refused.
This boundary comes from the shared [private archive reader](../../src/switchboard/connectors/_archive.py).

```bash
mkdir -p -m 700 "$HOME/.local/share/switchboard/imports/linkedin"
chmod 700 "$HOME/.local/share/switchboard/imports/linkedin"
cp -- "$HOME/Downloads/Connections.csv" "$HOME/.local/share/switchboard/imports/linkedin/Connections.csv"
chmod 600 "$HOME/.local/share/switchboard/imports/linkedin/Connections.csv"
```

Copy `messages.csv` or the downloaded ZIP archive into that same folder in the same manner.
Only a basename is accepted by the import tool, never a path.
Missing files and unsafe permissions are errors;
the connector does not repair permissions, move source files, or delete them.

## Supported export formats

Only the following comma-separated values (CSV) formats are declared.
Text must use `UTF-8` encoding; an initial byte order mark is accepted.
Standard CSV quoting, doubled quotes, and embedded newlines are supported.
Header comparison ignores case and repeated whitespace, but does not guess different field names or languages.
Unknown headers, duplicate canonical headers, missing required headers, and incorrect row widths are refused.
One malformed supported member rejects the entire import before any records or summary events are stored.

### Connection snapshots

Direct `Connections.csv` files require `First Name`, `Last Name`, and `Connected On`.
Recognized optional columns are `URL` or `Profile URL` for the same field, `Email Address`, `Company`, and `Position`.
This includes the common seven-column format and older exports without a profile address.
Each connection needs at least one name and a connection-date value.

An optional `Notes:` preamble is supported before the header.
After this marker, single-field note rows and the standard note beginning `When exporting your connection data` are skipped.
The header must appear within the first 16 logical CSV rows, counting blank rows and preamble rows.
Other preambles are unsupported.

### Message snapshots

Direct `messages.csv` files require `CONVERSATION ID`, `FROM`, `TO`, `DATE`, and `CONTENT`.
Recognized optional columns are `CONVERSATION TITLE`, `SENDER PROFILE URL`, `RECIPIENT PROFILE URLS`,
`SUBJECT`, and `FOLDER`.
The singular `RECIPIENT PROFILE URL` aliases the plural field.
This covers the common ten-column format and the older seven-column form without title or profile addresses.
Each message needs a conversation identifier and date; content may be empty for an attachment-only historical row.
Attachments themselves are not imported.

Names, recipient strings, profile addresses, dates, and folder names are retained as supplied.
Dates are not converted, sorted chronologically, or assigned an inferred time zone.
Recipient strings are not split into verified accounts, and messages are not classified as authenticated incoming or outgoing traffic.
Optional absent columns become empty strings.

### Archives and limits

A ZIP archive may contain one `Connections.csv` and one `messages.csv`, including nested directories and case variations of those basenames.
Other files are ignored.
Duplicate supported basenames, even in different directories, are refused.
Recognized files must still pass their declared header checks.
Archives are inspected in memory, never extracted.
Unsafe names, links, encrypted members, and unsupported compression are refused.

The import-file limit is 20,000,000 bytes.
Archives have at most 1,000 entries and at most 20,000,000 aggregate inflated CSV bytes, including ignored CSV datasets.
There are at most 50,000 imported records across both datasets, 20,000 characters per field, and 256 characters per conversation identifier.
Each CSV has at most 50,017 logical rows including blanks and its header.
These are local safety limits, not promises about LinkedIn's export format.
Oversized fields are rejected, not truncated; search previews never replace the complete stored field values.
Localized exports, other datasets, non-CSV message formats, and future unrecognized header changes require an explicit parser update rather than silent partial interpretation.

## Tools and approval binding

Tool arguments and results use JavaScript Object Notation (JSON).
Tier names below are literal core policy identifiers.

| Tool                      | Tier    | Behavior                                                       |
| ------------------------- | ------- | -------------------------------------------------------------- |
| `linkedin_import_export`  | `DRAFT` | Import an owned historical file by basename.                   |
| `linkedin_search`         | `READ`  | Search local imported records, not a live account.             |
| `linkedin_read`           | `READ`  | Read one complete bounded record by local numeric `id`.        |
| `linkedin_draft_post`     | `DRAFT` | Save immutable text, visibility, actor, and version locally.   |
| `linkedin_drafts`         | `READ`  | List previews, or read exact draft detail with `draft_id`.     |
| `linkedin_publish_post`   | `ACT`   | Attempt the exact approved own-profile post once, if enabled.  |

For example, importing a normal filename needs only:

```json
{"filename": "messages.csv"}
```

A renamed direct CSV file additionally needs `format` set to `connections` or `messages`.
That override is not accepted for a ZIP archive.
Search accepts `query`, `kind` (`connection` or `message`),
`conversation_id`, and `limit` from 1 to 50.
Its default limit is 20; results are newest local identifiers first,
not newest message dates.
Search previews bound each string to 240 characters and report `truncated`,
`historical`, `untrusted_content`, and `preview_only`.
Use `linkedin_read` for full bounded fields.

`linkedin_draft_post` takes `text` and optional `visibility`, either `PUBLIC` (default) or `CONNECTIONS`.
The local text limit is 3,000 characters.
Blank text, invalid Unicode characters, and control characters other than newlines and tabs are refused.
Text is preserved exactly, including surrounding whitespace and newlines; there is no hidden rewriting or template expansion.

Each draft gets a unique identifier and a digest over that identifier, exact text, visibility, actor, and pinned version.
Stored draft rows cannot be updated, including through ordinary database updates.
Corrections require a new draft; this connector has no edit or delete tool.
An empty configured `author_urn` permits local-only drafts, whose `publish_args` is null.
Configure your actor and create a new draft if you later want to publish one.

`linkedin_drafts` lists at most 20 previews by default, with no executable publish arguments in those previews.
Pass `draft_id` to the same tool for full text, delivery state, and exact `publish_args`.
Copy that complete object to `linkedin_publish_post`; it requires every one of these fields:

```json
{
  "draft_id": "the-32-character-local-identifier",
  "draft_hash": "the-64-character-content-digest",
  "text": "The exact stored text, not the preview.",
  "visibility": "PUBLIC",
  "author_urn": "urn:li:person:YOUR_ACTUAL_PERSON_ID",
  "api_version": "202610"
}
```

The identifiers above are explanatory placeholders, not executable values.
An editable draft identifier alone cannot authorize publication.
The handler compares every argument to the immutable row, verifies its digest, and requires the current configured actor and version to match.
It also checks that settings did not change while resolving the token.
The request uses the claimed actor and exact approved text, never a newly substituted profile or body.
Policy metadata callbacks use only schema-checked arguments; they do not read mutable drafts, resolve credentials, or perform account lookups before the handler.

Keep the example `decision = "ask"` rule.
Assistant and automation requests then wait for human approval.
The core treats a direct human command invocation as consent, and an operator can deliberately change policy; the connector does not override those core semantics.
Its separate disabled-by-default publication switch still applies even to an approved or policy-allowed call.
`allow_recipients` checks the approved author identifier as the target profile, not the viewers of a public post.

The approval summary includes the draft identifier, full digest, author, visibility, character count, and a short preview.
Inspect the complete stored arguments, not just that preview, before approving on your own machine:

```bash
switchboard approvals show APPROVAL_ID
switchboard approvals approve APPROVAL_ID
```

## Optional official publication setup

The LinkedIn application programming interface (API) uses app registration and OAuth 2.0 authorization.
This connector implements neither the authorization flow nor token refresh.
It accepts an operator-provided access-token secret reference, resolved only when publishing, or when running enabled credential diagnostics.
Never put the token itself in configuration, tool arguments, source files, command history, or approval text.
Use your password manager or a private launch environment to provision `LINKEDIN_ACCESS_TOKEN`, or use the core's owned mode-`600` secret-file support and a `file:` reference.

On your own machine:

1. Register your own application in the [LinkedIn Developer Portal][developer-portal].
1. Enable the **Share on LinkedIn** product and obtain authorization for your own member account with `w_member_social`, following the [official access instructions][getting-access].
1. Obtain the actual app-authorized person identifier through the official profile mechanism available to your application, as linked from the [post schema][post-schema].
   Set `author_urn` to `urn:li:person:<actual-id>`.
   Do not derive it from a vanity profile address, display name, exported sender, or guessed account identifier.
   If your application cannot establish the correct identifier, leave publication disabled.
1. Provision the token through the configured secret reference.
   This connector does not verify account ownership offline; you must supply your own authorized account, and LinkedIn must enforce the token's authority.
1. Set the example `allow_recipients` to that same exact own-person identifier.
   Keep `decision = "ask"`.
1. Keep `api_version = "202610"` explicitly pinned; it was the default official documentation version reviewed on 2026-11-11.
   Review supported versions before changing it; there is no automatic version negotiation.
1. Only then enable `publishing_enabled` on your own machine and create a fresh draft under those settings.

`w_member_social` is listed as an open publishing permission.
That does not guarantee every application, token, version, or endpoint combination will be accepted.
`r_member_social` is restricted to approved users and is not requested or used here.
Open publishing does not imply live inbox, feed, or message-reading access.
Expired tokens and missing app entitlements are operator problems, not reasons to scrape or bypass permissions.

The [official Posts API][posts-api] replaces `ugcPosts`.
This implementation sends exactly one fixed `POST https://api.linkedin.com/rest/posts` request without redirects, with bearer authorization, `LinkedIn-Version`, `X-Restli-Protocol-Version: 2.0.0`, and JSON content headers.
It supplies `author`, verbatim `commentary`, the approved `visibility`, `distribution` with `MAIN_FEED` and empty target/channel lists, `lifecycleState: PUBLISHED`, and `isReshareDisabledByAuthor: false`.
There is no media or reshare content.
Provider formatting of commentary is not validated locally; rich mentions and special formatting are not implemented, and a local draft is not proof that LinkedIn will accept or render every character as intended.

## Delivery state and failure handling

Before any web request, an immediate database transaction claims the immutable draft.
A draft can have only one attempt, and a unique index permits only one `IN_FLIGHT` publication across this connector, including other processes using the same database.
Two approvals cannot create two sends.
Claims are committed before transport.

- `DRAFT`: no attempt has been claimed.
- `IN_FLIGHT`: an attempt was committed, but no final outcome was durably recorded.
- `PUBLISHED`: status `201` from the exact endpoint plus one valid `x-restli-id` identifying a `share` or `ugcPost`.
- `REFUSED`: an explicit status from `400` through `499`, except `408`.
  Only the numeric status is exposed.
- `UNKNOWN`: transport failure, status `408`, server failure, redirect, another unexpected status, or success without a reliable post identifier.
  No response body or transport error text is returned or stored.

Missing confirmation is never success.
Every claimed draft is permanently consumed, even after an explicit refusal.
Unknown delivery must be checked manually in your own profile; do not automatically copy and republish its content.
This connector intentionally has no live read-back, automatic retry, or reconciliation command of its own.
The core action journal records an unknown publication as `uncertain`; `switchboard actions resolve` records what you found there, but it does not change the draft's state or publish anything.
An interrupted process may leave `IN_FLIGHT` behind; treat that as uncertain delivery too.
It blocks other publication attempts until deliberate operator reconciliation, not merely a restart.
Local reading, imports, and draft creation remain available.
No other process should still be publishing when operator maintenance is considered, and deleting an attempt to force a retry is not a safe reconciliation procedure.

## Storage, events, and retention

The shared archive store saves normalized records, not another raw source-file copy.
A snapshot identity includes the file checksum, parser version, and selected dataset context, but not the filename.
A repeated snapshot is a no-op.
Overlapping snapshots deduplicate identical canonical records while preserving the number of repeated identical rows within a snapshot.
Filenames, import timestamps, and row indexes are not part of record content.
Identity is the complete canonical row plus its occurrence within that snapshot, not a provider message identifier.
Indistinguishable repeated rows across different snapshots retain the largest occurrence count observed, not proof of which occurrence is new.
Edited rows are additional historical records, not verified updates or deletions; missing rows in a later snapshot do not delete prior records.
No identity-only merge is attempted.
Imports and their single summary event commit together; imported messages never emit individual live-action events.

These are the only connector events; all have source `linkedin`:

- `linkedin.imported`: `{datasets, file, records, inserted, historical: true}`, once per new snapshot.
- `linkedin.draft_created`: `{draft_id, draft_hash, author_urn, visibility, characters}`, without post text.
- `linkedin.post_published`: `{draft_id, draft_hash, author_urn, visibility, post_urn}`, only after confirmed delivery.

Draft and confirmed-publication events commit in the same transaction as their corresponding local state.
Refusals and unknown deliveries are visible through `linkedin_drafts` and the core action outcome, not a success-shaped event.
No events include credentials or full post bodies.

Source exports remain in the private import directory until you remove them yourself.
Imported records, full draft bodies, actor identifiers, hashes, and attempt state remain in the private SQLite database indefinitely; there is no connector retention job or per-record deletion tool.
Core event retention does not remove these tables.
Approval records retain exact text, and the core audit log retains short argument strings or longer prefixes with hashes, unless [`[audit] hash_only`](../DATA_LIFECYCLE.md#keeping-only-a-hash-of-message-text) lists `text` and `summary`.
Protect the entire data directory, database sidecars, source files, and backups as personal data.
Neither hashes nor local file permissions provide encryption or prove identity.

## Verification scope

Synthetic unit and integration cases cover the declared formats, idempotency, private-file boundary,
immutable drafts, approval argument binding, disabled publication, concurrent/duplicate approvals, configuration changes, confirmed creation, refusal, and ambiguous delivery.
Integration cases use `make_app` and `FakeHttp`, never a real account.
They require the main application to register the `linkedin` connector.

[Validation](../VALIDATION.md#current-status) records where these cases have run.
The setup commands above have not been run as written, and no authorization flow or live provider call has been made.
Provider compatibility, current application entitlements, and actual account publication remain unverified.

[account-download]: https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data
[developer-portal]: https://www.linkedin.com/developers/
[getting-access]: https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
[post-schema]: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema?view=li-lms-2026-10
[posts-api]: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-10
