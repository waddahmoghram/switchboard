# Local documents and notes

The `documents` connector reads explicitly named private folders on your own machine.
It lists candidate note filenames, searches literal text, and reads character pages.
It never writes notes, imports snapshots, builds an index, executes content, follows links, or contacts a remote service.
Registration validates configuration only: it does not check whether a folder exists, enumerate a directory, or read a file.

**Validation status: authored tests are UNRUN and uncollected.**
No application, example, server, personal folder, account, or device was exercised while implementing this connector.
Static checks do not establish runtime behavior, operating-system race resistance, or end-to-end assistant safety.

## What is supported

- Local, owner-only collections with explicit absolute paths and names such as `work` or `personal`.
- Visible, top-level regular files whose declared suffix is `.txt`, `.md`, or `.markdown`.
  Suffix matching ignores case.
  Each collection can allow only a subset.
  Subdirectories are never traversed, including subdirectories with supported-looking names.
- Strict [8-bit Unicode Transformation Format (UTF-8)](https://www.unicode.org/faq/utf_bom.html) text.
  One initial byte order mark is accepted and removed.
  Line endings, tabs, ordinary Unicode text,
  and Markdown source remain unchanged.
  Other control characters and invalid encoding are refused.
  A supported suffix is not proof of a document's format; printable encoded binary data is still just text.
- Case-sensitive literal substring search over complete file reads within the stated byte limits.
  There is no regular-expression, wildcard, query-language, semantic, fuzzy, filename, or case-folded search.
- Reading Markdown as plain text, not rendering it.
  Front matter, embedded instructions, links, images,
  attachment references, and application-specific links are just untrusted text.

There is no portable document format, office-document, rich-text, notebook, archive, image, audio, attachment, or handwriting parser.
There are no embeddings, model downloads, plug-ins, synchronization clients, watchers, automatic imports, or background scans.
Remote or network-mounted filesystems are unsupported.
Export a reviewed text copy yourself if another application owns the original format.
A copied note is current only relative to that local copy; this connector cannot verify that it matches a live service or original vault.

## Folder and file boundary

Use a small dedicated collection, not your home directory, entire working folder, workspace, repository, download folder, or an entire note vault.
There is no implicit home or workspace discovery.
Home, current working directory, and their ancestors are rejected as collection roots; arbitrary other workspace locations cannot be inferred, so choosing a dedicated folder remains your responsibility.

The implementation requires local Unix-style descriptor-relative operations with directory and no-follow flags; Linux is the intended platform.
It fails closed if those flags are absent.
It does not silently fall back to an unsafe path reader.
File permissions alone do not describe access-control lists, privileged users, mount behavior, remote filesystems, or every filesystem's consistency guarantees.
Use an ordinary local disk without additional access grants.
There is no claim of a sandbox against another process running as you.

- A collection must be a real directory owned by the process user with mode `700`, exactly.
  The connector opens every component from the filesystem root with no-follow flags, refusing symbolic links in any ancestor as well as a symbolic-link root.
  It does not resolve a link into an allowed location.
- Files must be owned regular files with mode `400` or `600`, exactly, and exactly one hard link.
  Shared,
  executable, unreadable, special-mode, foreign-owned, hardlinked, symbolic-link, directory, device, socket,
  and named-pipe sources are refused.
  Nonblocking opens avoid waiting on a pipe before its type is checked.
- Tool calls choose a configured collection name and a basename, never a path.
  Hidden names, slashes, backslashes, colons, control characters, invalid filename text, and names exceeding 255 encoded bytes are refused.
  No environment-variable or home-shorthand expansion occurs in configured paths.
- There are at most 16 named collections.
  Names use lowercase letters, digits, hyphens, and underscores,
  start with a letter, and contain at most 32 characters.
  Duplicate names and normalized paths are refused.
  Unknown settings fail rather than quietly enabling recursion or indexing.

The core still creates its normal private database and audit records.
"Read-only" describes source documents, not the absence of all application writes.
Reading may update filesystem access times.
There is no document body, search index, import marker, historical snapshot, or directory inventory persisted by this connector.

## Set up a small own-machine collection

These are **recommended manual steps, UNRUN here**.
Review paths and existing files before using them.
There is no account login, external subscription, paid aggregator, document conversion service, or credential.

1. Make dedicated folders and create one synthetic note to test later.
   The following shell commands stop on failure, refuse overwriting an existing example, and do not recursively change any existing folder.

   ```bash
   set -eu
   set -C
   umask 077
   install -d -m 700 "$HOME/SwitchboardNotes/work"
   install -d -m 700 "$HOME/SwitchboardNotes/personal"
   printf '%s\n' '# Example note' 'Release checklist: check the draft.' \
       > "$HOME/SwitchboardNotes/work/example.md"
   chmod 600 "$HOME/SwitchboardNotes/work/example.md"
   ```

1. Merge [the complete example configuration](../config/documents.toml) into your private Switchboard configuration.
   Replace `/home/your-user` with your actual absolute home path.
   Keep existing connector, assistant-exposure, instructions, and policy settings; do not paste a second copy of an existing table.
   The example names two collections.
   Remove the `personal` collection if you do not want to expose it.
   No configured folder is created automatically.
1. Ensure the application knows the connector mapping `"documents": "switchboard.connectors.documents"` in `KNOWN_CONNECTORS`, and include `documents` in `[switchboard].connectors`.
   Application registration is a separate core integration change.
   There are no connector migrations to apply, services to provide, events to wire, or jobs to start.
1. Restart any running Switchboard process after changing the configuration file.
   A process reads its file at startup; this connector does not watch or reload it.
   Every request rereads the already loaded configuration table, so an in-memory root removal is honored, but a disk edit alone does not revoke a running process.
1. Later, with runtime execution approved, inspect tools and roots and try a deliberately harmless note:

   ```bash
   switchboard tools
   switchboard doctor
   switchboard call documents_collections
   switchboard call documents_list \
       --args '{"collection":"work","limit":10}'
   switchboard call documents_search \
       --args '{"collection":"work","query":"Release checklist","limit":5}'
   switchboard call documents_read \
       --args '{"collection":"work","filename":"example.md","max_chars":1000}'
   ```

   `doctor` checks root paths and permissions only.
   It does not enumerate filenames, test note decoding, check search coverage, create folders, or repair permissions.
   An apparently healthy root is not proof that its contents are supported, readable, within limits, or safe to send to an assistant.

For an existing note application, manually copy only selected plain-text notes into a dedicated collection, then review their permissions and content.
Do not symlink an application vault, use hard links, broaden a home directory's permissions, or recursively apply `chmod` to make the connector work.
To include a nested folder, configure that specific private folder as another collection instead of enabling recursion; recursion is not an available setting.

## Tools and response interpretation

All four tools have `Tier.READ`.
There is no indexing or importing tool at `Tier.DRAFT`, no source-writing capability at any tier, no public named service, no connector event, and no scheduled job.

| Tool                    | Required arguments       | Optional arguments                  | Meaning                              |
| ----------------------- | ------------------------ | ----------------------------------- | ------------------------------------ |
| `documents_collections` | None                     | None                                | Configured names, formats, limits    |
| `documents_list`        | `collection`             | `offset`, `limit`                   | Sorted candidate filename page       |
| `documents_search`      | `collection`, `query`    | `offset`, `limit`                   | One hit per matching scanned file    |
| `documents_read`        | `collection`, `filename` | `offset`, `max_chars`               | Current decoded text character page  |

Each result has `untrusted_content = true`, `source = "live_filesystem"`, `cached = false`, and `scope = "one_collection_top_level"`.
These fields describe how the result was obtained, not its author, authenticity, factual accuracy, or freshness relative to an external application.
Collection discovery has `availability_checked = false` and does not disclose configured absolute root paths.
`observed_at` records the call's observation time, and `freshness = "call_time_only"` explicitly limits the observation to that call.
These fields must not be interpreted as a fresh authorization or filesystem check when an old result is replayed.

### Listing

Listing enumerates at most the configured number of directory entries plus one overflow sentinel, then sorts eligible names in case-sensitive filename order.
All entries count, including hidden files, unsupported formats, directories, and unsafe files.
Overflow fails the entire listing or search with an error and no partial filenames or snippets.
This avoids sorting an unbounded directory or presenting an arbitrary prefix as a complete inventory.
A known-basename read does not enumerate, so it can still read one allowed file in an overfull collection.

`total` counts eligible metadata candidates, not validated text documents.
`text_validated = false` is intentional: invalidly encoded or binary content with a supported suffix may appear in a filename listing.
`listing_complete = true` means enumeration completed within this bounded, top-level scope, not that every file in the folder was exposed or every candidate can be decoded.
`skipped` contains counts by reason.

`offset` defaults to zero and `limit` to 20; the hard maximum page size is 50.
`next_offset = null` ends the eligible filename pages.
Edits and additions between calls can change ordering or invalidate offsets.
No stable cursor, consistent snapshot, filename identity, or permanent document identifier is promised.

### Search

`query` is a nonempty string of at most 256 characters.
Matches use a literal, case-sensitive substring check on decoded content, with no Unicode normalization.
Newlines and punctuation are literal.
A word in a filename alone does not match.
There is one result per matching file, using its first occurrence; repeated occurrences are neither individually enumerated nor ranked.

Search starts from the same sorted candidate set on every call.
It reads complete files until the aggregate byte budget stops it.
It does not search prefixes of oversized files, malformed files, or a file that cannot fit in the remaining budget.
It stops at that file rather than skipping ahead to selectively search smaller later files.
Invalid files already read still consume the aggregate budget.

- `files_searched` counts successfully decoded complete files, including nonmatches.
- `bytes_read` reports actual bytes consumed, including reads later rejected.
- `matches_in_scanned_files` is not a whole-folder match count unless `coverage_complete` is true.
- `budget_exhausted = true` and `coverage_complete = false` make a stopped scan explicit.
  Unsafe, oversized,
  unavailable, changed, or invalid text candidates also make coverage incomplete.
- Unsupported names and nested folders are outside declared scope.
  Their exclusion does not by itself make supported-scope coverage incomplete.
  Always inspect the declared formats, scope, and `skipped` counts.
- `next_offset` pages matching files from this capped scan, not bytes or occurrences.
  It never resumes beyond the byte-budget cutoff.
  A null next offset or an empty result is not proof of no other matching text.
- Each `snippet` begins at `match_offset`, is at most 400 characters, and shares the total returned-text budget.
  `snippet_truncated` reports omitted prefix or suffix text.
  After the aggregate returned-text budget is spent, later results may have an empty snippet with `text_omitted = true`.
  `returned_text_chars` counts snippet characters; filenames and bounded metadata are separate.

If coverage is incomplete, narrow the collection manually, split it into smaller explicitly configured collections, or increase a configured limit within its hard ceiling after considering privacy and performance.
Do not claim complete full-text search, full note-vault coverage, or the absence of a phrase from an incomplete result.
There is no hidden index to query for files the live scan could not inspect.

### Reading and pagination

Reads validate and decode the entire bounded file before returning any character page.
Invalid or oversized notes fail instead of returning a seemingly valid prefix.
`offset` is a decoded-character index, not a byte offset, line number, or stable cursor.
`max_chars` defaults to 4,000 and is additionally clamped to `max_return_chars`.
`size_bytes` includes an initial encoding mark; `total_chars` does not.

`has_more` identifies a remaining suffix.
Pass `next_offset` into a later call to request it.
`text_truncated` is true whenever either a prefix or suffix was omitted.
An offset beyond the current end returns empty text and a null next offset.
Every page rereads the live file: changes between pages may alter content, length, and offsets.
Copy a reviewed immutable note yourself if consistent multi-page reading matters.

## Bounds and configuration reference

All four numeric settings are positive whole numbers; booleans, unknown settings, zero, negative values,
and values above the hard ceiling are rejected.
Limits cannot be raised through a tool call.

| Setting             | Default   | Hard ceiling | Bound applied                                               |
| ------------------- | --------- | ------------ | ----------------------------------------------------------- |
| `max_entries`       | 256       | 1,000        | Every entry enumerated in one collection                    |
| `max_file_bytes`    | 262,144   | 1,048,576    | Complete file content bytes                                 |
| `max_search_bytes`  | 2,097,152 | 8,388,608    | Actual aggregate bytes read by one search                   |
| `max_return_chars`  | 8,000     | 20,000       | One read page or the total of one search page's snippets    |

Enumeration reads at most `max_entries + 1` entry names to detect overflow.
A per-file read asks for at most `max_file_bytes + 1` bytes so a growing or underreported file cannot evade the actual byte-size check.
The extra byte is an overflow sentinel, never returned as text.
During search that sentinel request is also bounded by the remaining aggregate budget: actual aggregate reads never exceed `max_search_bytes`.

The reader conservatively requires room for an end-of-file check before attempting a file.
A file whose reported size equals the remaining search budget is not searched, even if its real content would fit exactly.
Reading no bytes from such a file still sets `budget_exhausted = true`.
This small boundary loss is intentional.
Large invalid text and already-read nonmatches count just as much as matching files.
No compressed content is expanded, so there is no decompression multiplier or attachment-byte exception.

Tool input ceilings are 50 results per page, 1,000 listing/search offset, 1,048,576 read offset, 20,000 requested read characters, and 255 filename characters, with the separate stricter 255 encoded-byte filename check.
At most 16 configured collection names are returned.
Filenames, result counts, and metadata are independently bounded; `max_return_chars` is not a bound on the serialized size of the complete response.
A repeated caller can accumulate many pages or issue many requests; these are per-request bounds, not a lifetime disclosure cap.

## Live reads, revocation, and failure handling

No index means no stale-content cache to invalidate or delete.
Each content call reopens the configured root through its absolute component chain and validates ownership, modes, file type, hard-link count, advertised size, actual bytes, and decoded text.
It checks the opened file and its current basename before accepting text.
Before returning accumulated results, it rechecks the loaded allowlist and limits, reopens and compares root identity, and checks every observed returned or successfully searched file for changes.

Observed deletion, replacement, permissions changes, ownership changes, or edits before those final checks invalidate the call instead of returning accumulated stale text.
A candidate that cannot be read during search is skipped with a reason and incomplete coverage.
If an already observed file becomes invalid before the final validation, the whole request fails.
An unavailable or revoked root never falls back to a snapshot.
Generic errors do not include root paths, file bodies, or original decoder errors.

There is no atomic multi-file filesystem snapshot.
A same-user process can race the final checks, edit a file between pages, or change a file after a result has already left the process.
The checks are best-effort live validation, not a kernel-enforced revocation barrier.
No connector can erase content already received by an assistant, copied into a conversation, or retained by a client.

The core approval store is a separate historical-retention boundary: approving a tool stores its returned result, not just its arguments, in the core database.
`approvals_status` can replay that old note text without calling this connector or rechecking its root.
`observed_at` and `freshness` make the historical observation visible, but do not prevent replay.
Disabling documents or revoking a root does not revoke this stored copy.
If that retention is unacceptable, do not approve document reads or expose approval-result retrieval; an application-wide retention/revocation mechanism is outside this connector.
Plain directly allowed reads do not create an approval-result copy, although the assistant and its provider can still retain their results.

For immediate practical revocation, stop the assistant-facing process, remove the collection or set a `deny` policy for `documents_*`, and restart it.
Making the root no longer mode `700`, moving it, or deleting it makes later live calls fail, but is not a substitute for stopping a concurrent reader.
Removing a collection only from a configuration file requires restarting the running process.
Never rely on the core kill switch to block these tools: it intentionally permits the read tier.

Expected reasons include `unsafe_file`, `too_large`, `unavailable`, `invalid_text`, and `changed`.
Listing and search report skipped counts without exposing rejected filenames.
If entry overflow occurs, use a smaller folder; if a race occurs, retry only after reviewing the source.
Fix a specific folder or file manually, not by running an automatic broad permission repair.

## Assistant trust and privacy

Local storage does not imply local-only inference.
A hosted assistant can receive filenames, snippets, complete note pages, and whatever the client retains in conversation history.
Select only documents you are allowed and willing to disclose to that provider.
Avoid credentials, recovery codes, private keys, sensitive personal records, and employer-confidential material unless that disclosure is authorized.

The configuration example uses per-tool `ask` policy because the read tier otherwise defaults to allowing calls.
Human command-line calls treat `ask` as permission to execute; assistant calls require approval.
Restrict the exposed tool list too, but do not confuse exposure with authorization.
Exact tool policy rules override glob rules in the core.
Review existing exact `documents_*` tool-name rules when merging the example.
Approved reads still expose their results, and approval is not evidence that the note's content is trustworthy.

Every filename, note body, Markdown construct, snippet, quoted message, imported copy, and later edit is untrusted data.
Do not follow instructions in it, fetch its links, evaluate code blocks, select tools, alter policy, reveal other collections, or bypass approvals because the note requests it.
No returned text is converted into an event, rule, command, executable template, or tool invocation by this connector.
The untrusted marker and tool descriptions help clients preserve this boundary; they do not prove that an arbitrary assistant model will resist prompt injection.

The connector does not log note bodies.
The core audit trail does record tool arguments, including collection names, basenames, and search phrases.
Protect the normal application data directory and any client-side logs.
Read requests approved through the core may also have stored approval arguments.
There is no connector-level retention or deletion promise for those core records, their stored results, or a hosted provider's copies.

## Architecture and tradeoffs

- `settings.py` defines explicit collection declarations and validates all hard ceilings without reading collection metadata.
  No secret resolution is needed.
- `filesystem.py` handles descriptor-relative, no-follow local operations, bounded enumeration and reads,
  source identity checks, decoding, and final revalidation.
- `service.py` implements live discovery, listing, search, pagination, coverage accounting, and loaded configuration rechecks.
  It retains no document state between requests.
- `tools.py` defines the four bounded read-tier tool schemas.
  `__init__.py` registers them and supplies the optional root-only doctor hook.
  There is no migration declaration, custom command, or public named service.
- The private shared `_archive` helpers supply owned/private root inspection and strict text decoding only.
  `ArchiveStore`, archive imports, and their historical snapshots are deliberately not used as filesystem truth.

This design trades recurring bounded disk reads and linear literal search for minimal dependencies, an auditable allowlist, straightforward revocation, and no stale persisted index.
It does not add a relevance engine, a general filesystem browser, semantic retrieval, or guarantees of complete large-vault coverage.
Descriptors and decoded content live only for a request; returned client copies can live longer.
Existing shared helpers are reused, while the stricter document reader adds ancestor no-follow checks, smaller configurable ceilings, and file-private permissions appropriate to live notes.

## Authored test inventory and later validation

The unit and integration test sources are synthetic and **UNRUN**, including parameterized cases for configuration, owned private folders and files, foreign ownership, mode changes, unsupported formats, hidden names, traversal, symbolic links in roots and ancestors, hard links, directories, named pipes, simulated device/socket metadata, invalid text, real byte/count limits, underreported metadata, literal matching, snippet budgets, pagination, live edits, deletion, allowlist removal, and source revocation.

Integration sources guard network clients, address resolution, socket sends/connections, and process-launch entry points, and use `FakeHttp`.
They author assertions for registration without source access, read-only source behavior, absent snapshot tables/events/jobs/services, audit-body minimization, and core policy denial.
Those are intended assertions, not observed runtime results.

If runtime testing is separately authorized later, use a project-local test directory rather than the system temporary directory.
The following is an **unexecuted proposed command**, not validation evidence:

```bash
pytest --basetemp=./.test-data/documents \
    tests/unit/test_documents_settings.py \
    tests/unit/test_documents_filesystem.py \
    tests/unit/test_documents_tools.py \
    tests/integration/test_documents_flow.py
```

The integration sources require the core `KNOWN_CONNECTORS` mapping described above.
No test creates a real account, invokes an application server, needs a model, or intentionally contacts a network service.
Static syntax parsing, type checking, linting, configuration parsing, and Markdown checks cannot establish that these authored runtime assertions pass.
