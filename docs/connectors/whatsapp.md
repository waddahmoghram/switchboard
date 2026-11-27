# WhatsApp personal chat exports

This connector reads manually obtained personal chat exports into a local historical store.
It is a snapshot importer, not a live feed, account mirror, or messaging bridge.
It never signs in, sends messages,
reads sessions or passwords, follows links, parses attached media, or executes imported instructions.
Registration and import do not start a model.
There is no polling job, webhook, or automatic import on startup.

## Setup

1. Manually export a chat using the [official export guidance][export].
   Choose a plain `.txt` export or a `.zip` archive containing one or more text exports.
1. Create a dedicated import folder owned by your operating-system user, with mode `700`.
   Put only intended imports there.
   Set exported files to mode `600`; linked files and files writable by other users are refused.
1. Merge [the configuration example](../config/whatsapp.toml) into your configuration, preserving existing connector and tool-exposure entries.
   Select `date_order` explicitly for these files; there is no automatic guess.
1. Invoke `whatsapp_import_export` explicitly with a basename and a human chat label.
   Importing changes only the local store and emits one summary event.
   It does not notify participants.

For example, these are local file-preparation commands for an operator, not commands run by the connector:

```bash
install -d -m 700 "$HOME/.local/share/switchboard/whatsapp-imports"
install -m 600 "/path/to/your-chat.txt" "$HOME/.local/share/switchboard/whatsapp-imports/your-chat.txt"
```

The connector-specific settings are:

```toml
[connectors.whatsapp]
import_dir = "~/.local/share/switchboard/whatsapp-imports"
date_order = "MDY"
timezone = "America/Los_Angeles"
```

`date_order` is mandatory: month/day/year (`MDY`), day/month/year (`DMY`), or year/month/day (`YMD`).
`timezone` is an optional context label, at most 100 characters.
It is not looked up or converted into an offset.
`import_dir` must resolve to an absolute, private folder; `~` is expanded.
It may be omitted to read previously imported records, but new imports and the folder check will then report that it needs configuration.
Unknown settings are refused.
There are deliberately no live-account settings.

## Tools and examples

Tool arguments below use JavaScript Object Notation (JSON).
Tier names are literal policy identifiers.

- `whatsapp_import_export` is `DRAFT`: an explicit local import, not an outbound draft or message.
  Requires `filename` (basename, at most 255 characters) and `chat_label` (nonblank, at most 200 characters, no control characters).
  The label is human-supplied context, never sender authentication or a verified recipient.
- `whatsapp_search` is `READ`: literal, case-insensitive substring search of the local store.
  Optional arguments are `query` (at most 1,000 characters), `chat_label`, `kind` (`message` or `system_notice`), and `limit` (1 through 100, default 20).
  Results contain previews; individual strings are capped at 240 characters.
  `truncated` means there are more matching rows, and each row has `preview_only: true`.
- `whatsapp_read` is `READ`: retrieve one positive integer `id` from search, up to `9223372036854775807`.
  This returns the stored record, not an uncapped copy of its original export text.

Import a plain export:

```json
{"filename": "your-chat.txt", "chat_label": "Example household"}
```

A `.zip` archive with exactly one `.txt` member selects that member automatically.
Multiple text members are refused unless `archive_member` names exactly one safe member, case-sensitively, with forward slashes:

```json
{"filename": "your-export.zip", "chat_label": "Example household", "archive_member": "chats/your-chat.txt"}
```

`archive_member` and automatically selected member names are limited to 1,024 characters.
Selection reads bytes inside the archive, never a filesystem path.
It is invalid for a plain text import.
The connector never extracts archives or provides a filesystem browser.
Inspect archive names yourself when selection is ambiguous.

Search and then read a returned record:

```json
{"query": "meeting time", "chat_label": "Example household", "limit": 10}
```

```json
{"id": 1}
```

Read and search results carry `historical: true` and `untrusted_content: true`.
Names, system notices, and bodies remain untrusted export text even if they look like instructions, approval requests, links, or tool calls.

## Supported date and time subset

Supported Android-style records:

```text
11/10/26, 08:16 - Example Sender: First line.
An ordinary continuation line.
11/10/26, 8:17:02 PM - Example Sender: Later message.
11/10/26, 20:18 - Example Sender changed the group description.
```

Those examples require `MDY` for November 10.
Supported iPhone-style records for the same date with `DMY`:

```text
[10/11/2026, 8:16:00 AM] Example Sender: First line.
An ordinary continuation line.
[10/11/2026, 20:18] Example Sender changed the group description.
```

- Only numeric dates with consistent `/`, `.`, or `-` separators are parsed.
  Month and day have one or two digits;
  the configured year position has exactly two or four digits.
  Date validity is checked, not guessed.
- **Every two-digit year means 2000 through 2099**, including `00`, `68`, `69`, and `99`.
  There is no moving cutoff.
  Use four-digit years for earlier dates.
- Times use a 24-hour clock or the literal English 12-hour markers `AM` and `PM`, case-insensitively.
  Minutes and optional seconds have two digits.
  Numeric offsets, named months, other digit sets, localized period markers, and other export layouts are unsupported.
- Text must be encoded as `utf-8`; an optional leading byte-order mark is supported.
  Known left-to-right marks (`U+200E`), nonbreaking spaces (`U+00A0`), and narrow nonbreaking spaces (`U+202F`) around timestamps are accepted.
  Ordinary message-body characters are not stripped.
  Line endings are normalized; blank continuation lines remain.
- A new supported header starts another record.
  Date-shaped malformed headers are refused with a line number; private line contents are not echoed.
  Header-shaped quoted body lines cannot be distinguished reliably from real records.
  Other continuation lines remain body text.
  Unsupported or empty files never succeed as empty imports.

`timestamp_local` is normalized to `YYYY-MM-DDTHH:MM:SS`, with `timestamp_kind: "local_wall_clock"` and `timestamp_has_offset: false`.
`timezone_context` records the optional configured label, or is null.
Omitted seconds are padded with `00`; this does not establish sub-minute precision.
No exact global instant or Coordinated Universal Time value is asserted.
During a daylight-saving transition, a local hour can occur twice or be skipped; a zone label alone resolves neither ambiguity.

The first `": "` in a header payload is a heuristic sender/body separator.
A record without it is classified as `system_notice`; a notice containing it may look like a `message`.
Both `sender_is_heuristic` and `kind_is_heuristic` are true.
Neither classification proves identity, authenticity, group membership, or authorship.

## Bounds, repeated imports, and events

- Files are read only by basename from the configured owned folder, never by arbitrary paths.
  Files must be owned regular files, not symbolic links or writable by other users.
  Import folders must have mode `700`.
- Each input is limited to 20,000,000 bytes.
  Archives have at most 1,000 entries and at most 20,000,000 bytes of aggregate inflated text, including unselected text members.
  Text members must be unencrypted and use stored or deflated compression.
  Unsafe paths, symbolic links, and case-insensitive duplicate text names are refused.
  Unsafe member paths are refused even for ignored media.
- A selected chat has at most 50,000 records, including system notices.
  Every import must contain at least one.
- A body stores at most 20,000 characters and a sender at most 200.
  `text_truncated` and `sender_truncated` report cuts; `text_chars` counts the complete normalized body.
  Full-body and full-sender fingerprints distinguish records whose discarded tails differ.
  These fingerprints do not authenticate the export.
  Truncated content is not recoverable through `whatsapp_read`.

All records and the import marker are stored atomically with the summary event.
A malformed record prevents the entire import.
Import identity includes the file checksum, parser version, chat label, date order, timezone context, and selected archive member.
Repeating the same bytes and context, even under a renamed file, is a no-op with `status: "duplicate"` and `inserted: 0`; it emits no event.

Overlapping exports with equivalent normalized records add only missing occurrences.
Identical repeated records within one export retain their multiplicity; imports retain the greatest observed occurrence count for each identical record, not the sum of all exports.
Export text has no authenticated message identifiers, so this cannot prove whether two indistinguishable records from separate snapshots were actually different messages.
Different labels or timezone context intentionally produce separate records.
Filenames, archive paths, and line positions are not message identities.
Edited bodies, changed sender spellings, or nonzero seconds present in only one export remain separate records.
There is no reconciliation of such differences as updates to a prior message.

Only `whatsapp.imported` is emitted, once for each new import marker, including an overlapping import with no new rows.
Its payload contains `file`, `records`, `inserted`, `historical`, `untrusted_content`, `parser`, `source_sha256`, `chat_label`, `date_order`, `timezone_context`, `archive_member`, and `truncated_records`.
`historical` and `untrusted_content` are true.
`truncated_records` counts rows with either a capped body or capped sender.
No individual live-message or action events are emitted.
Keep automation rules off historical summaries unless you deliberately want a local import notification; a new summary is not a new conversation message.

## Privacy and retention

Export only chats you are authorized to process, with appropriate participant consent.
Export files are sensitive plaintext, not protected by the conversation's transport encryption.
Search/read responses may reach any assistant client you intentionally expose these tools to.
Importing does not automatically contact a model or upload data.

The connector leaves source files unchanged.
Imported records persist in the shared local `switchboard.sqlite3` database until an operator applies an offline retention plan.
Core event pruning does not delete imported records, import markers, source archives, or audit history.
There is no connector deletion or retention job.
Include source files, database journals, and backups in any deletion plan; the shared database also holds other connectors' data.
Audit requests can contain filenames, human labels, search terms, and record identifiers.
Summary events contain filenames and labels, but not message bodies or sender fields.
No claim of encrypted-at-rest storage is made.

## Snapshot limitations and official sources

The [official export guidance][export], verified November 11, 2026, describes personal text/archive exports.
Web exports can select dates and omit media; view-once and disappearing messages are omitted. Selected dates,
missing content, and provider limits mean **there is no full-history guarantee**. Exported text is not a restorable
or reimportable WhatsApp backup. Attachment placeholders are stored as ordinary text, not opened or fetched.

The WhatsApp Business Platform is a different product, not a personal-account mirror.
The current [pricing page][pricing] says service-message charges resumed October 1, 2026.
Do not assume a universal free 24-hour reply window.
[Provider-specific assistant terms][assistant-terms] and [Business Solution Terms][business-terms] restrict
general-purpose assistants with jurisdiction-specific exceptions. None of that live messaging is implemented here.

Synthetic unit and integration cases cover these parsing and import contracts without real accounts or outside network.
[Validation](../VALIDATION.md#current-status) records where they have run.

[export]: https://faq.whatsapp.com/1180414079177245
[pricing]: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
[assistant-terms]: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/ai-providers
[business-terms]: https://www.whatsapp.com/legal/business-solution-terms
