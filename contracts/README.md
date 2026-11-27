# Cross-engine contract

This specification is the shared behavior of the Python reference engine and the C++ native engine where their capabilities overlap.
C and Rust use the C++ engine, so they inherit it.
Contract revision: **2**.
The revision increases whenever a normative rule is added or changed, and each case file's `contract_revision` names the revision it demonstrates.
Revision 2 adds [Audit redaction](#audit-redaction) and leaves the rules of revision 1 unchanged.

A rule here is normative only for overlapping features.
It does not claim the native engine implements every Python connector.
Shared fixtures in [cases](./cases/) are evaluated by a runner for each engine; known intentional differences are listed in [Known divergences](#known-divergences) instead of being hidden.

Fixtures, configuration and requests use JavaScript Object Notation (JSON).
No fixture contacts an account,
device, or network service.

## Outcome envelope

Every tool call returns an object with `status` and `tool`, plus `message`, `approval_id` and `data` when relevant.
`status` is one of:

| Status    | Meaning                                                                  |
| --------- | ------------------------------------------------------------------------ |
| `ok`      | The handler ran and returned a report. It is **not** proof of an effect. |
| `pending` | An approval was created. No handler ran.                                 |
| `denied`  | Policy, the kill switch, a budget, or the duplicate guard refused it.    |
| `invalid` | The tool is unknown, not exposed, or the arguments failed the schema.    |
| `error`   | The handler failed, definitely or with an unknown outcome.               |

Every executed non-read call also returns `action`: `{"id": "<32 lowercase hex>", "state": "<state>"}`.
Read calls, pending approvals, and refusals before execution have no `action`.

## Action journal

Each engine records every **non-read** execution attempt (draft and act tools, and any spend tool a Python policy explicitly lets run; the native engine never runs spend tools) before the handler runs.
Python stores it in `action_journal`; the native engine in `native_actions`.
Storage formats are engine-local.

| State                    | Entered when                                                                 |
| ------------------------ | ---------------------------------------------------------------------------- |
| `in_flight`              | Recorded atomically with the duplicate check, immediately before the handler |
| `succeeded`              | The handler returned a report without declaring its effect unknown           |
| `failed`                 | The handler asserted the effect did not happen                               |
| `uncertain`              | The effect might or might not have happened                                  |
| `reconciled_applied`     | The operator confirmed an uncertain effect happened                          |
| `reconciled_not_applied` | The operator confirmed an uncertain effect did not happen                    |

### Classifying a result

1. A handler returns a JSON object with top-level `"effect_unknown": true`: **uncertain**.
2. A handler returns otherwise: **succeeded**.
   Adapter-level confirmation remains in the report.
3. Python `UncertainOutcomeError`, or a native `Error` whose code is `uncertain`, `transport`, `limit`,
   `cancelled` or `deadline`: **uncertain**.
   These native codes can occur after a request was sent.
4. Any other expected tool failure (Python `ToolError`; any other native `Error` code): **failed**.
   Raising it asserts that the effect did not happen; adapters must not use it after a possibly delivered write.
5. Any unexpected exception: **uncertain**.
6. A process that ends mid-call leaves `in_flight`.
   A sweep marks an `in_flight` attempt **uncertain** once its `deadline_at` is more than 60 seconds in the past.
   Sweeps run before each duplicate check, before each reconciliation, when an engine starts, and on each runner tick.

The owner process may still record its own late result for an attempt that a sweep marked uncertain, until the operator reconciles it.
No other process can.

### Duplicate guard and reconciliation

Before recording a new attempt, the engine looks for an attempt with the same tool and the same canonical argument digest in state `in_flight` or `uncertain`.
If one exists, the call returns `denied` and names the blocking action.
Approved requests are checked the same way when they execute; a blocked approval finishes as failed.

Only the operator reconciles: the Python command line, the Python broker operator socket, the native command line, the native broker operator socket, the C interface, or the Rust wrapper.
Assistants cannot.
Only `uncertain` attempts can be reconciled, with a note of 1 to 500 printable characters.
Either resolution lifts the guard.
Reconciling records a human decision; it never repeats, reverses, or verifies the effect.

Read tools are not journaled and may be retried.
The guard exists for writes, where blindly repeating a possibly completed request could send a second message or apply a second change.

Retention may delete `succeeded`, `failed` and reconciled entries after their retention period.
It never deletes `in_flight` or `uncertain` entries.

## Deadlines and cancellation

| Setting (`[switchboard]` / `switchboard`) | Range  | Default | Python | Native | Meaning                                       |
| ----------------------------------------- | ------ | ------- | ------ | ------ | --------------------------------------------- |
| `tool_timeout_seconds`                    | 5-3600 | 120     | yes    | yes    | Deadline for one tool execution               |
| `job_timeout_seconds`                     | 5-3600 | 300     | yes    | yes    | Deadline for one polling-job run              |
| `job_workers`                             | 1-16   | 4       | yes    | no     | Concurrent polling lanes in the Python runner |

An attempt's `deadline_at` is its start plus the applicable timeout.
Engines apply the remaining time to web requests made inside the call and fail them once it passes.
Neither engine forcibly interrupts arbitrary handler code: Python threads cannot be killed, and the native engine does not abandon a write midway.
A deadline that expires during a write produces an uncertain attempt, never an automatic retry.

Native cancellation (`Runtime::cancel`, `sb_runtime_cancel`) aborts web transfers of the call in progress and does not affect later calls.
A cancelled write is uncertain.
The Python runner stops scheduling new jobs on shutdown and lets running jobs finish or reach their deadlines.

## Typed outcomes for C and Rust

The C interface maps an outcome to `status` and `effect`:

| Condition                                                            | `effect`                |
| -------------------------------------------------------------------- | ----------------------- |
| Read tool, `pending`, `denied`, `invalid`, or error before execution | `SB_EFFECT_NONE`        |
| `action.state` is `uncertain`                                        | `SB_EFFECT_UNKNOWN`     |
| `action.state` is `failed`                                           | `SB_EFFECT_FAILED`      |
| `succeeded` and `data.confirmed` is `true`                           | `SB_EFFECT_CONFIRMED`   |
| `succeeded` and `data.confirmed` is `false`                          | `SB_EFFECT_UNCONFIRMED` |
| `succeeded` without a boolean `data.confirmed`                       | `SB_EFFECT_RETURNED`    |

`SB_EFFECT_CONFIRMED` means a software readback matched the requested state.
It never proves a physical effect.
Adapters that read back state set a boolean `confirmed`; adapters that cannot tell whether a write was delivered return `"effect_unknown": true` or raise the uncertain error.

## Storage, retention and backup

Each engine refuses state written by a newer schema instead of silently downgrading it.
A backup is a consistent online copy, and verification opens the copy read-only, checks database integrity, recognized schema versions, and the audit chain, then reports the audit head.
Verification does not prove that the providers' or devices' current state matches the backup.

Python retention covers core tables (`[retention]`) and connectors that provide a retention hook (`[retention.connectors]`).
Native retention covers core tables.
Neither purges open journal entries.

## Isolation

Approval separation is only meaningful with separate operating-system accounts.
The broker owns the configuration, credentials and database.
The assistant's account reaches only the client socket, which exposes the protocol's tool listing and calls.
The operator's account reaches the operator socket, which approves, denies, reconciles and toggles the kill switch.
Each socket checks the peer's numeric user identifier with the operating system's peer-credential facility, in addition to file permissions.

Running the assistant and the operator under one account restores the old boundary: that account can reach both sockets.
Neither engine claims a sandbox in that configuration.

### Operator socket protocol

Both brokers accept the same newline-delimited operator requests, so one operator client works with either engine.
A request is an object with `op`, an optional `request_id` (string or integer, echoed back), and only the fields listed; unknown fields are refused.

| `op`          | Fields                                                    | Result                                                    |
| ------------- | --------------------------------------------------------- | --------------------------------------------------------- |
| `status`      | none                                                      | Engine, kill switch, pending approvals, open actions      |
| `approvals`   | none                                                      | Pending approvals without arguments                       |
| `approval`    | `id`                                                      | One approval, including its exact stored arguments        |
| `approve`     | `id`                                                      | The execution outcome                                     |
| `deny`        | `id`                                                      | The denial result                                         |
| `actions`     | `state`, `limit` (1-100), or `id` alone                   | Journal entries; `id` returns one entry with arguments    |
| `reconcile`   | `id`, `resolution` (`applied` or `not_applied`), `note`   | The reconciled entry                                      |
| `kill_switch` | optional boolean `enable`; omitted only reports           | The kill-switch state                                     |

A response is `{"ok": true, "op": ..., "request_id": ..., "result": ...}` or `{"ok": false, "op": ..., "request_id": ..., "error": {"code": ..., "message": ...}}`.
Review `approval` before `approve`: approving executes exactly the stored arguments.

## Audit redaction

Both engines redact each audit record before appending it, so the hash chain covers only the redacted copy.
Python redacts every field except `ts` and `kind`, and also the arguments quoted in the default approval summary of a tool without its own `summarize`.
The native engine redacts every field except `event` and `at`, and also the arguments it stores in its action journal, which it scrubs but never hashes; Python's journal stores exact arguments.
Native audit records carry no approval summary.
Both engines keep exact arguments in the approval queue, because approving executes exactly what is stored.
A rule change applies to new records only, so older logs still verify.

A value under a credential key becomes `[redacted]`, whatever its type, and keys themselves are never changed.
A key is a credential key when either rule holds:

* It contains, in any letter case, `pass`, `secret`, `token`, `authorization`, `authorisation`, `cookie`, `credential`, `apikey` or `privatekey`; the last two may have `_` or `-` between their words, as in `api_key` and `private-key`.
* One of its segments, in lower case, is `otp`, `totp`, `hotp`, `mfa`, `2fa`, `pin`, `session`, `private`, `auth`, `cvv` or `cvc`.
  The segments are the successive matches of the regular expression `[A-Z]+[0-9]*(?![a-z])|[A-Z]?[a-z0-9]+`, backtracking included: `userPin` gives `user` and `Pin`, `HTTPServer` gives `HTTP` and `Server`, and `ABC12def` gives `ABC1` and `2def`.
  So `card_pin` and `session_id` are credential keys, while `pinned`, `spinner`, `mapping`, `sessions` and `author` are not.

Every other string, at any depth, is scrubbed by the rules below, in order.
Each rule replaces every match in the output of the rule before it, scanning left to right without overlaps.
A match that its rule keeps, such as a short bearer token without a digit, still consumes its text, and the scan resumes after it.

1. A private key in Privacy-Enhanced Mail (PEM) form, from `-----BEGIN … PRIVATE KEY-----` or `… PRIVATE KEY BLOCK-----` to the first such end line or the end of the text, becomes `[redacted:private-key]`.
2. A JSON Web Token (JWT), three dot-separated parts of which the first two start with `eyJ`, becomes `[redacted:jwt]`.
3. A password in a web address, as in `postgres://alice:…@db.example.com`, becomes `[redacted]`.
4. The value of a query parameter after `?`, `&` or `;` named `access_token`, `refresh_token`, `id_token`, `token`, `api_key`, `apikey`, `key`, `secret`, `client_secret`, `password`, `passwd`, `auth`, `sig`, `signature`, `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `X-Goog-Signature` or `X-Goog-Credential` becomes `[redacted]`.
5. A labeled value, as in `password: hunter2` or `"api_key": "k-1"`, becomes `[redacted]`, inside its quotes when it is quoted at both ends.
   The labels are `password`, `passwd`, `passphrase`, `passcode`, `secret`, `client_secret`, `api_key`, `access_token`, `refresh_token`, `auth_token`, `token`, `otp` and `pin`, where each `_` may also be `-` or absent.
6. The value of an `Authorization`, `Proxy-Authorization`, `X-Api-Key` or `X-Auth-Token` header, or of a `Cookie` or `Set-Cookie` header that holds a `name=value` pair, becomes `[redacted]` up to its closing quote or the end of the line.
   The header name, the quotes and a leading `Basic`, `Bearer`, `Digest`, `Negotiate` or `token` are kept.
7. A token of 16 or more characters after `Bearer` becomes `[redacted]` when it has a digit or at least 32 characters.
8. A GitHub token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` and `github_pat_`) becomes `[redacted:github-token]`.
9. A Slack token (`xoxa-`, `xoxb-`, `xoxp-`, `xoxr-` and `xoxs-`) becomes `[redacted:slack-token]`.
10. An Amazon Web Services (AWS) access key identifier, `AKIA` or `ASIA` and 16 capitals or digits, becomes `[redacted:aws-key-id]`.
11. A key with a digit that starts with `sk-`, `sk_live_`, `sk_test_`, `rk_live_`, `rk_test_` or `AIza` becomes `[redacted:api-key]`.
12. The secret half of a Telegram bot token, as in `123456789:…`, becomes `[redacted]`.
13. A run of 32 or more hexadecimal characters with at least one digit and one letter becomes `[redacted:hex]`.
14. A run of 40 or more letters, digits, `-` and `_` becomes `[redacted:base64]` when it mixes capitals, lower case and digits in pieces that average under three characters.
    The pieces are the matches of `[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+`.

Rules 3 to 7 ignore letter case; the others are case-sensitive.
The regular expressions of `_VALUE_PATTERNS` (rules 1 to 12) and `_RANDOM_RUNS` (rules 13 and 14) in [audit.py](../src/switchboard/audit.py), with the functions they call, state each rule exactly, including its boundaries and letter case.
The native engine implements them with hand-written scanners in [audit.cpp](../native/src/audit.cpp).

Rules 13 and 14 skip strings under an identifier key: a key whose last segment is `id`, `ids`, `uid`, `uuid`, `hash`, `digest`, `sha1`, `sha256`, `sha512`, `checksum`, `fingerprint`, `head`, `binding`, `etag`, `revision`, `action` or `target`.
The exemption passes into arrays, each key of a nested object sets it again from that key alone, and a top-level string has none.

`hash_only` names up to 64 fields whose text is kept only as a length and a hash: Python's `[audit] hash_only` and the native `audit.hash_only`.
Each name is a nonempty string without surrounding whitespace, and names match keys in any letter case.
Every string at any depth under a named key is scrubbed and then becomes `[N chars, sha256:H]`.
`N` counts the scrubbed text's Unicode code points, and `H` is the first 16 lowercase hexadecimal digits of the Secure Hash Algorithm 256-bit (SHA-256) digest of its 8-bit Unicode Transformation Format (UTF-8) bytes.
Numbers, booleans and null under a named key are kept, and a credential key under a named key still gives `[redacted]`.

Python also shortens a string of more than 300 code points to its first 120, `…` and `[N chars, sha256:H]`, keeps the first 50 items of a list, and replaces values nested more than six levels deep with `[nested too deeply]`.
The native engine keeps strings and lists whole and refuses values nested more than 64 levels deep with the error code `limit`.

The native scanners use American Standard Code for Information Interchange (ASCII) character classes on UTF-8 bytes, while Python's `\s`, `\S`, `\b` and case-insensitive matching follow Unicode.
The engines agree on every ASCII input; on other text they differ in only three ways, each a row of [Known divergences](#known-divergences):

* `Audit whitespace`: Python's `\s` and `\S` also treat Unicode whitespace such as U+00A0, U+0085 and U+2028 as whitespace, which can end or refuse a value in rules 3 to 6.
* `Audit word characters`: Python's `\b` treats non-ASCII letters and digits as word characters, so a scheme, header name or `Bearer` right after one is not at a word boundary in rules 3, 6 and 7.
* `Audit case folding`: Python's case-insensitive rules 3 to 7 and credential keys also match `ſ` (U+017F) as `s`, the Kelvin sign (U+212A) as `k`, and `İ` (U+0130) and `ı` (U+0131) as `i`, and Python's `str.lower()` lowers every non-ASCII capital, such as `Ñ`, when it compares `hash_only` names with keys.

Key segmentation, the piece rule and the case-sensitive rules 1, 2 and 8 to 14 use only explicit ASCII classes and any-character matches, so the engines treat non-ASCII characters in them alike.
`Audit structural limits` in the same table records the length, list and depth limits above, and `Journal arguments` the native journal's scrubbing.

## Known divergences

| Topic                                           | Python                                                                     | Native                                                                        |
| ----------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Approval identifiers                            | 8 lowercase hexadecimal characters                                         | 32 lowercase hexadecimal characters                                           |
| Stored timestamps                               | Text, `YYYY-MM-DDTHH:MM:SS+00:00`                                          | Integer seconds; rendered `YYYY-MM-DDTHH:MM:SSZ`                              |
| Job concurrency                                 | Bounded worker lanes                                                       | Serial, deadline- and budget-bounded                                          |
| Connector coverage                              | Full reference set                                                         | Local-first subset; see the native guide                                      |
| Schema annotations (`schema.json`)              | `$id` allowed; text annotations must be strings; `$schema` must be 2020-12 | `$id` refused; annotation values and `$schema` unchecked                      |
| Schema patterns (`schema.json`)                 | `re` on characters: Unicode `\d`, lookbehind                               | ECMAScript `std::regex` on bytes: `\d` is 0-9, `.` is one byte, no lookbehind |
| Policy rule names (`policy.json`)               | Names without `*`, `?` or `[` match exactly                                | Every name also matches as a case-insensitive pattern                         |
| Hourly budget timing (`policy.json`)            | Checked at request and at execution                                        | Checked at execution only, so an approval is still queued                     |
| Spend tier configuration (`policy.json`)        | Denied by default; a configured `ask` runs on a human call or approval     | Always denied, whatever the configuration                                     |
| Exposure enforcement (`exposure.json`)          | Protocol layers only, not `Dispatcher.call`                                | Also `Runtime::call` for the assistant origin                                 |
| Protocol error flag (`exposure.json`)           | `isError` only for `denied`, `error`, `invalid`                            | Also for an `action.state` other than `succeeded`                             |
| Protocol structured content (`exposure.json`)   | `structuredContent` repeats the envelope                                   | Text content only                                                             |
| Approval result (`approvals.json`)              | `approvals_status` `result` is the handler's `data`                        | The whole outcome envelope                                                    |
| Unknown approval lookup (`approvals.json`)      | `ok` with `data.status` `unknown`                                          | `invalid`, raised by the handler                                              |
| Approved-call journal origin (`approvals.json`) | `approval`                                                                 | `human`                                                                       |
| Task search (`tasks.json`)                      | Case-insensitive                                                           | Case-sensitive                                                                |
| Task timestamp input (`tasks.json`)             | Any offset form `datetime.fromisoformat` reads; errors from the handler    | Whole seconds with `Z`, `+HH:MM` or `-HH:MM`; wrong lengths are `invalid`     |
| Inactive task reminders (`tasks.json`)          | `reminder_delivery` stays `not_queued`                                     | `suppressed_inactive`                                                         |
| Audit structural limits                         | Strings over 300 code points shortened; lists cut to 50; depth capped at 6 | Strings and lists kept whole; nesting deeper than 64 refused with `limit`     |
| Audit whitespace (`audit-redaction.json`)       | `\s` and `\S` also count Unicode spaces, such as U+00A0 and U+2028         | Only ASCII whitespace: 0x09 to 0x0D, 0x1C to 0x1F and the space               |
| Audit word characters (`audit-redaction.json`)  | `\b` counts non-ASCII letters and digits as word characters                | Only ASCII letters, digits and `_` are word characters                        |
| Audit case folding (`audit-redaction.json`)     | `re.I` and `str.lower()` also fold non-ASCII letters, such as `ſ` and `Ñ`  | Only `A` to `Z` fold, in patterns, credential keys and `hash_only` names      |
| Journal arguments                               | Stored exactly                                                             | Scrubbed by the audit redaction rules, without `hash_only`                    |

Runners compare instants and semantic outcomes, not storage formatting.
Any further divergence a fixture exposes must be added here with the fixture that demonstrates it.

## Fixtures and runners

[cases](./cases/) holds the shared fixtures: the `schema`, `policy`, `approvals`, `journal`, `exposure`, `tasks` and `audit-redaction` case files (format `switchboard-contract-cases`, `version` 1) and the synthetic tools both runners register.
Each case lists engine-neutral steps, such as `call`, `approve`, `reconcile`, `rpc` and `redact`, with the outcome they must produce; the [fixture reference](./cases/README.md) documents every key.
A step on which the engines legitimately differ carries `divergence` with a topic from the table above, a reason and per-engine expectations.
`approvals.json` and `tasks.json` also demonstrate the `Approval identifiers` and `Stored timestamps` rows.

A case carries `known_violation` instead when an engine does not yet meet a normative rule.
That runner reports an expected failure and fails once the case passes, so the marker is removed when the engine is fixed.
No case carries one at present.

Run both runners on your own machine; these commands have not been run as written:

```bash
uv run pytest -m contracts tests/contracts
cmake -S native -B build/native -DSWITCHBOARD_BUILD_TESTS=ON
cmake --build build/native --target native-test-contracts
ctest --test-dir build/native -L contracts --output-on-failure
```

The Python runner is [tests/contracts/test_contracts.py](../tests/contracts/test_contracts.py).
The native runner, [native/tests/contracts.cpp](../native/tests/contracts.cpp), reads the directory compiled in as `SWITCHBOARD_CONTRACTS_DIR` and lists every failed case.
[Validation](../docs/VALIDATION.md#current-status) records where both runners have run.
