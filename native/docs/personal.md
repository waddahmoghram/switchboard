# Native local documents, tasks, and reminders

## Validation status and scope

This is the C++20 implementation in [personal.cpp](../src/personal.cpp), registered by
`register_personal(Context&, Registry&)`. It independently checks `context.enabled("documents")`
and `context.enabled("tasks")`; enabling one does not enable the other.

**Tests are authored, UNRUN, and uncollected.** No project code, test, example, scheduler, server,
personal folder, account, or device was executed or accessed during authoring. The permitted compiler
syntax check is static evidence only: it does not prove linking, database compatibility, filesystem
race resistance, concurrency, scheduler timing, policy execution, or notification delivery.

Merge [personal.example.json](../config/personal.example.json) into the native configuration, keeping
unrelated connector, policy, and exposure settings. The example deliberately exposes all ten tools;
reduce that list before use if an assistant should see fewer capabilities. Replace its placeholder
collection path with a small, deliberately selected local folder. Configuration examples are UNRUN.
No credentials, accounts, secret lookup, network requests, or source-folder creation are required.

The native engine has its own private data directory and SQLite database. Do not point it at Python
state. There is no import, migration, synchronization, or shared-state compatibility with the Python
connectors. Source documents are read-only; the engine's own task, approval, event, and audit records
are not read-only.

## Tools and exact request fields

All request objects reject unknown fields, including apparently harmless unsupported options such as
`path`, `recursive`, `url`, `provider`, `recurrence`, or `notify`. Runtime policy still applies; direct
handler access is trusted engine code, not another approval boundary.

| Tool                    | Tier    | Required fields          | Optional fields                                                |
| ----------------------- | ------- | ------------------------ | -------------------------------------------------------------- |
| `documents_collections` | `read`  | None                     | None                                                           |
| `documents_list`        | `read`  | `collection`             | `offset`, `limit`                                              |
| `documents_search`      | `read`  | `collection`, `query`    | `offset`, `limit`                                              |
| `documents_read`        | `read`  | `collection`, `filename` | `offset`, `max_chars`                                          |
| `tasks_status`          | `read`  | None                     | None                                                           |
| `tasks_list`            | `read`  | None                     | `status`, `query`, `list_name`, `after_id`, `limit`, `overdue` |
| `tasks_read`            | `read`  | `id`                     | None                                                           |
| `tasks_add`             | `draft` | `title`                  | `notes`, `list_name`, `due_at`, `remind_at`                    |
| `tasks_update`          | `draft` | `id`, `revision`         | `title`, `notes`, `list_name`, `status`, `due_at`, `remind_at` |
| `tasks_delete`          | `act`   | `id`, `revision`         | None                                                           |

`tasks_update` additionally requires at least one editable field. Deletion uses the core's
action-tier approval policy, with an exact identifier-and-revision summary. An approval for an older
revision cannot delete a later edit. Approval is not a general permission to delete another task.

## Private document boundary

Only visible, top-level basenames ending in a collection's declared `.md`, `.markdown`, or `.txt`
suffix are supported. Suffix matching ignores the case of letters A-Z. Markdown is plain text, not rendered
content. Links, embedded instructions, front matter, and attachment references are untrusted text,
never commands to follow or remote content to retrieve.

Text uses strict [8-bit Unicode Transformation Format (UTF-8)](https://www.unicode.org/faq/utf_bom.html).
One initial byte order mark is removed. Tabs and line endings are preserved; other characters below
32, and characters 127 through 159, are rejected. Invalid encodings, surrogates, overlong encodings,
and incomplete characters are rejected before any text page is returned. A printable binary encoding
with a supported suffix is still only text; suffixes do not prove file provenance or format.

Each configured root must be an explicit absolute local folder. Home shorthand, environment expansion,
network-style paths, dot path components, colons, backslashes, control characters, the home folder,
the working folder, and their ancestors are rejected. The home-folder exclusion uses the process's
`HOME`; it is not discovery of every user's home or every workspace. Selecting a genuinely dedicated
folder remains the operator's responsibility.

The shared helpers require an owned root with mode `700`. Observed symbolic links in any root-path
component are refused. Reads require owned regular files; this connector additionally requires exact
file mode `400` or `600` and one hard link. Basenames cannot be hidden, contain either path separator,
contain a colon or forbidden control character, or exceed 255 encoded bytes. Directories, pipes,
devices, sockets, links, and executable or shared-mode files cannot supply note text. Linux and ordinary
local filesystems are the intended environment; network mounts are unsupported, not automatically
detected. Do not broadly change permissions on a home directory or existing note vault.

Registration validates configuration only. It never enumerates a collection, reads a note, checks a
root's availability, creates a source folder, or resolves a secret. Collection inventory returns names,
formats, and limits, not absolute root paths. `availability_checked = false` is intentional.

Every access uses the current in-memory allowlist. Each content request rereads its source and rechecks
authorization and observed path safety before returning; search also checks inventory again.
There is no document cache, persistent descriptor, index, historical snapshot, watcher, embedding, or
background document job. Permission revocation, source deletion, or allowlist removal must not fall
back to an earlier result. Changes observed during rechecking fail the request instead of returning
accumulated old text. These checks do not promise an atomic filesystem snapshot or a sandbox against
a racing process with the same user privileges. Previously returned copies cannot be recalled.
Editing a configuration file alone does not reload an already running process; restart it to apply
disk configuration changes.

### Document limits

Names use `[a-z][a-z0-9_-]{0,31}`. Configure 1-16 uniquely named collections with unique normalized
paths, each no longer than 4,096 encoded bytes. `extensions` is a nonempty, duplicate-free subset of
the three supported lowercase suffixes. Unknown collection or document settings fail explicitly.

| Document setting   | Default   | Hard maximum | Meaning                                                                  |
| ------------------ | --------: | -----------: | ------------------------------------------------------------------------ |
| `max_entries`      | 256       | 1,000        | All directory entries, including hidden, unsupported, and unsafe entries |
| `max_file_bytes`   | 262,144   | 1,048,576    | Entire file, before decoding or removing its initial mark                |
| `max_search_bytes` | 2,097,152 | 8,388,608    | Aggregate read budget, including live verification reads                 |
| `max_return_chars` | 8,000     | 20,000       | One read page or all snippets on one search page                         |

All four settings must be positive integers, not booleans. Tool requests cannot raise them. Listing
and search use `offset = 0`, `limit = 20` by default, with maxima 1,000 and 50 respectively. Queries
are nonempty literal strings of at most 256 encoded bytes. Reading uses `offset = 0`,
`max_chars = 4000` by default; their hard maxima are 1,048,576 and 20,000, and the requested page is
also clamped to `max_return_chars`.

All document offsets, snippet positions, and returned-text budgets count Unicode scalar values,
not encoded bytes, displayed glyphs, lines, or stable file positions. A later page rereads the live
file and may have different offsets after an edit.

### Listing and reading

`documents_list` enumerates at most the entry cap plus an overflow sentinel. Exceeding the cap fails
the entire request, not a purportedly complete prefix. It returns sorted `notes` containing candidate
`filename` values, `total`, `offset`, `next_offset`, `enumerated_filenames`, and `skipped` counts for
`unsupported_name` and `unsafe_or_unavailable`. Regular-file, link-count, and mode checks filter
candidates; ownership is also checked by the reader when content is requested.

`listing_complete = true` means bounded enumeration completed, not that every file is a supported
readable document. `text_validated = false` means invalid or oversized text may still appear as a
candidate basename. Pagination is over candidates, not a consistent snapshot.

`documents_read` returns `text`, `size_bytes`, `total_chars`, `offset`, `next_offset`, `has_more`, and
`text_truncated`. It validates the whole bounded file before slicing; invalid or oversized files
cannot yield a plausible valid prefix. An offset beyond the current end returns empty text with no
next offset. A known-basename read does not enumerate, so it remains possible in an overfull folder.
Two bounded reads, including verification, consume at most `2 * (max_file_bytes + 1)` content bytes.

### Literal search and honest coverage

Search is a case-sensitive literal substring search of decoded text, without normalization, filename
matching, regular expressions, ranking, or query syntax. Each matching file contributes at most one
result at its first occurrence. `offset` pages matching files from this call's bounded scan; it does
not resume an unsearched byte-budget suffix.

Search reserves room for two worst-case file reads before attempting the next candidate:
`2 * (max_file_bytes + 1)` must fit in the remaining budget. After a successful first read it charges
the actual bytes and reserves only that observed size, plus an overflow sentinel, for verification.
Verification refuses growth beyond the observed size and compares the complete contents' digest.
Empty files use a one-byte verification ceiling. Invalid text still consumes known read bytes.
A failed first read is conservatively charged `max_file_bytes + 1`, even when no bytes were read.
Consequently, a conservative budget stop can occur with unused capacity after verification refunds.
Small `max_search_bytes` values can prevent searching any file; this is reported, not silently widened.

| Search field                | Interpretation                                                                          |
| --------------------------- | --------------------------------------------------------------------------------------- |
| `files_searched`            | Complete, decoded files successfully live-verified, including nonmatches                |
| `candidate_files`           | Entries with declared supported basenames, before text and safety validation            |
| `files_not_searched`        | Supported-name entries not attempted after a budget stop                                |
| `matches_in_scanned_files`  | Matching files in the processed scope, not necessarily the whole collection             |
| `read_bytes_known`          | Bytes returned by successful helper reads, including verification and rejected text     |
| `read_budget_charged_bytes` | Known bytes plus conservative worst-case charges for failed reads                       |
| `failed_read_bytes_unknown` | Actual bytes consumed by one or more failed reads are unknown                           |
| `budget_exhausted`          | A candidate was not attempted because its conservative reservation would not fit        |
| `skipped`                   | Counts for `unsupported_name`, `unavailable_or_unsafe_or_oversized`, and `invalid_text` |
| `coverage_complete`         | No budget cutoff and no unreadable, unsafe, oversized, or invalid supported-name file   |
| `next_offset`               | Another page of matching files within this scan, or `null`                              |
| `text_truncated`            | At least one returned snippet omitted part of its source                                |

Each result includes `filename`, character `match_offset`, `snippet`, `snippet_truncated`,
`text_omitted`, and `untrusted_content`. Snippets start at the match and use at most 400 characters,
sharing `max_return_chars`. Later snippets can be empty after that return budget is used.
`returned_text_chars` counts snippet characters only. Filenames, metadata, enumeration, and filesystem
status checks are separate from the byte and returned-text budgets.

Unsupported names are outside declared scope. Unsafe or invalid supported-name entries prevent a
complete-coverage claim. An empty result or `next_offset = null` does not prove a phrase is absent if
coverage is incomplete. Inspect every coverage and truncation field before making that inference.
Entry-cap or live-verification failures return an error, not partial snippets.

Document results carry `untrusted_content = true`, `cached = false`, `scope = "one_collection_top_level"`,
`freshness = "call_time_only"`, and `observed_at`. `source` is `configuration` for collection inventory
and `live_filesystem` otherwise. These describe the observation, not authorship, safety, factual truth,
an external application's freshness, or renewed authorization when an old result is replayed.

## Persistent local tasks

Titles are bounded to 200 encoded bytes, notes to 4,000, and list names to 64. Titles and list names
must be nonblank single lines; surrounding ordinary spaces (`U+0020`) are removed. Notes preserve line breaks.
Task text must satisfy the same strict encoding and control-character rules, without removing an
initial mark. The default list name is `personal`, notes are empty by default, and new status is `open`.
Identifiers and revisions are positive integers no greater than `9007199254740991`.

`tasks_read`, creation, and update return the task fields, `revision`, `created_at`, `updated_at`,
`reminder_event_queued_at`, `reminder_delivery`, `notes_truncated`, and `untrusted_content`.
Increasing committed identifiers are not reused after deletion. An update or deletion must name the
exact current revision. Updates increment it even for a same-value edit; reminder polling does not.
Failed or stale operations do not silently overwrite newer data.

Due and reminder instants are independent, optional, and can be cleared with explicit `null`.
Accepted input is exactly `YYYY-MM-DDTHH:MM:SSZ` or `YYYY-MM-DDTHH:MM:SS+HH:MM` (also negative offsets),
with uppercase separators and whole seconds. Calendar dates and components must be real. Input years
are 1970-9999; the normalized instant must remain in that range. Offset hours range 00-23 and minutes
00-59; `-00:00`, fractional seconds, leap seconds, naive local times, date-only input, and natural
language are rejected. Results use normalized zero-offset timestamps ending in `Z`.

`tasks_list` defaults to `status = "open"`, `query = ""`, `after_id = 0`, `limit = 20`, and
`overdue = false`. Status can be `open`, `completed`, `cancelled`, or `all`; `list_name` is an exact
filter. Query is a case-sensitive literal substring of title or notes, at most 200 encoded bytes.
There are no wildcard or regular-expression semantics. `overdue = true` further requires an open
task with `due_at <=` the current instant. It does not imply a reminder exists.

Results are ordered by increasing identifier, capped at 100. `truncated` and `next_after_id` identify
another matching page; this is not snapshot pagination. Notes previews contain at most 180 encoded
bytes without splitting a character. Inspect `notes_truncated` or use `tasks_read` for full notes.
Returned task content is untrusted and may reach a hosted assistant.

### One-shot reminders are queued events

The `tasks.reminders` job polls every 60 seconds by default. `poll_interval_seconds` must be 30-3,600;
`max_reminders_per_tick` must be 1-500 and defaults to 100. Unknown task settings are refused.
The interval is registered at startup; restart after changing it. The current in-memory batch limit
is revalidated on each check. Disabling tasks in memory suppresses further checks and tool access.

Each poll selects only open, due `remind_at` values without a queue marker. It updates that marker
and emits `tasks.reminder_due` on the **same database connection and in one transaction**. Failure
rolls back the batch, its events, markers, and successful-check state together. Repeated polls cannot
resend a committed schedule. The batch limit drains a backlog over later polls, not one unbounded tick.

The event source is `tasks`. Reminder payloads contain `id`, `revision`, `title`, `due_at`, `remind_at`,
`untrusted_content = true`, and `delivery = "event_queued"`. Deduplication uses the identifier and an
internal schedule generation, not the mutable task revision. Creation and editing emit identifier-only
`tasks.changed` events containing `id`, `revision`, and `status`; deletion emits `tasks.deleted`
containing `id`, `revision`, and `deleted_at`. These mutations and events also commit together.

Completing or cancelling a task suppresses future queueing; it does not perform the task. Reopening
a task whose reminder was already queued does not resend it. Reopening an unqueued, now-overdue
schedule can queue it. Editing title, notes, list, status, or `due_at`, or supplying an equivalent
`remind_at` with another offset, preserves an existing queue marker. Only changing `remind_at` to a
different instant resets that marker and creates a new schedule. Clearing it cancels future queueing.

**Already queued events cannot be withdrawn.** Completion, cancellation, deletion, or rescheduling
does not retract earlier queue entries or copies. A later consumer may see an older task revision.
`reminder_delivery` is `event_queued`, `not_scheduled`, `suppressed_inactive`, or `not_queued`;
none proves receipt by a person.

`tasks_status` reports counts, the actual registered `poll_interval_seconds`, current batch limit,
current-process `job_error`, and durable `last_check = {checked_at, queued}` for the most recent
successful poll, or `null`. `last_check_is_liveness = false` is deliberate. Failed database writes
leave that record stale, and another process's scheduler error is not visible here.

No automatic phone, email, push, or other notification delivery exists in this module. It provides
no recurring schedules, external task-provider synchronization, natural-language date parsing,
external delivery retries, or exactly-once notification guarantee. A stopped or delayed scheduler
cannot meet reminder times; polling frequency is not a delivery deadline.

### Storage and retained copies

When and only when tasks are enabled, registration uses `CREATE TABLE IF NOT EXISTS` for
`native_tasks_items` and `native_tasks_reminder_state`. Index names are `native_tasks_reminders`
and `native_tasks_lists`. SQLite also maintains its ordinary identifier sequence. No Python table is
read or changed. The single successful-check row is replaced by each successful poll.

Deleting a task removes its row, not queued events (which can include old reminder titles), audit
records, approval arguments, backups, SQLite storage remnants, or assistant copies. There is no
automatic task-retention or secure-erasure promise. This connector persists no document bodies,
document index, scan inventory, or source snapshot; other engine logging and already returned copies
have their own retention. Source reads may update filesystem access times.

## Native versus Python, and authored coverage

The Python [documents guide](../../docs/connectors/documents.md) and
[tasks guide](../../docs/connectors/tasks.md) are reference material, not native commands or proof
that features were ported. Python command examples, service/doctor hooks, database migrations, Python
notification-rule integration, case-folded task search, and Python's character-based task-field
limits are not native features of this module. Native documents use conservative two-pass search
budget accounting and different coverage field names; do not assume Python response compatibility.
Neither implementation promises document conversion, embeddings, provider sync, or phone delivery.

Authored cases live in [personal.cpp tests](../tests/personal.cpp). They use scoped, private,
workspace-relative synthetic fixtures and a rejecting fake transport, not personal folders, a system
scratch directory, or live accounts. **Every function below remains UNRUN and uncollected.**

| Authored function                                    | Intended behavioral coverage, not observed results                                   |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `test_independent_registration_and_no_scan`          | Independent enablement, tool tiers, no source scan or transport                      |
| `test_configuration_rejection`                       | Unknown settings, invalid bounds, unsafe roots, duplicate roots, unsupported formats |
| `test_document_paths_links_and_private_modes`        | Traversal, symlink roots/ancestors/files, hard links, pipes, modes                   |
| `test_document_bounds_paging_and_invalid_text`       | Entry/file/page bounds, encoding, control characters, full-file validation           |
| `test_document_literal_search_and_budget`            | Literal/case-sensitive search, paging, snippet and two-pass byte budgets             |
| `test_document_revocation_and_live_reads`            | Fresh reads; between-call and in-call permission/allowlist revocation                |
| `test_task_limits_and_strict_instants`               | Text bounds, unsupported fields, strict calendars and offsets                        |
| `test_task_paging_search_and_revision_conflicts`     | Bounded previews, literal search, filters, stale edits/deletion                      |
| `test_task_persistence_and_increasing_ids`           | Reopened database, retained revisions, no identifier reuse                           |
| `test_reminder_idempotency_and_schedule_changes`     | One-shot generations, equivalent instants, unrelated edits, retained events          |
| `test_reminder_cancellation_completion_and_batching` | Inactive tasks, clearing/reopening, bounded backlog, disabled polling                |
| `test_reminder_and_mutation_rollback`                | Injected second-event failure, markers/check state, creation/edit/delete rollback    |
| `test_runtime_exact_revision_deletion_approval`      | Pending deletion, persisted exact arguments, stale/current approval, replay          |

Static compilation cannot establish these behavioral assertions, scheduling or locking under multiple
processes, actual policy replay, filesystem races, durability after a crash, or external delivery.

### Static checks performed during authoring

The C++20 syntax-only check of both sources was executed from the repository root and exited
successfully with no compiler diagnostics; it did not link or execute anything. The command shape
below uses a portable placeholder for the external dependency headers:

```bash
c++ -std=c++20 -Wall -Wextra -Wpedantic -fsyntax-only \
    -I native/include \
    -isystem "/path/to/validation-dependencies/include" \
    native/src/personal.cpp native/tests/personal.cpp
```

The example configuration was parsed as data. The four scoped files were checked for trailing
whitespace, tab indentation, and final newlines; C++ sources were checked for character values below 128.
Markdown fence balance and local file links were checked. These are limited static checks, not a
Markdown linter run, application configuration validation, test collection, a build, or runtime proof.
