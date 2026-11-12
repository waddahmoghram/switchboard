# Cross-engine contract

This specification is the shared behavior of the Python reference engine and the C++ native engine where
their capabilities overlap. C and Rust use the C++ engine, so they inherit it. Contract revision: **1**.

A rule here is normative only for overlapping features. It does not claim the native engine implements every
Python connector. Shared fixtures in [cases](./cases/) are evaluated by a runner for each engine; known
intentional differences are listed in [Known divergences](#known-divergences) instead of being hidden.

Fixtures, configuration and requests use JavaScript Object Notation (JSON). No fixture contacts an account,
device, or network service.

## Outcome envelope

Every tool call returns an object with `status` and `tool`, plus `message`, `approval_id` and `data` when
relevant. `status` is one of:

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

Each engine records every **non-read** execution attempt (draft and act tools, and any spend tool a Python
policy explicitly lets run; the native engine never runs spend tools) before the handler runs. Python stores
it in `action_journal`; the native engine in `native_actions`. Storage formats are engine-local.

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
2. A handler returns otherwise: **succeeded**. Adapter-level confirmation remains in the report.
3. Python `UncertainOutcomeError`, or a native `Error` whose code is `uncertain`, `transport`, `limit`,
   `cancelled` or `deadline`: **uncertain**. These native codes can occur after a request was sent.
4. Any other expected tool failure (Python `ToolError`; any other native `Error` code): **failed**. Raising it
   asserts that the effect did not happen; adapters must not use it after a possibly delivered write.
5. Any unexpected exception: **uncertain**.
6. A process that ends mid-call leaves `in_flight`. A sweep marks an `in_flight` attempt **uncertain** once
   its `deadline_at` is more than 60 seconds in the past. Sweeps run before each duplicate check, before each
   reconciliation, when an engine starts, and on each runner tick.

The owner process may still record its own late result for an attempt that a sweep marked uncertain, until
the operator reconciles it. No other process can.

### Duplicate guard and reconciliation

Before recording a new attempt, the engine looks for an attempt with the same tool and the same canonical
argument digest in state `in_flight` or `uncertain`. If one exists, the call returns `denied` and names the
blocking action. Approved requests are checked the same way when they execute; a blocked approval finishes
as failed.

Only the operator reconciles: the Python command line, the Python broker operator socket, the native command
line, the native broker operator socket, the C interface, or the Rust wrapper. Assistants cannot. Only
`uncertain` attempts can be reconciled, with a note of 1 to 500 printable characters. Either resolution
lifts the guard. Reconciling records a human decision; it never repeats, reverses, or verifies the effect.

Read tools are not journaled and may be retried. The guard exists for writes, where blindly repeating a
possibly completed request could send a second message or apply a second change.

Retention may delete `succeeded`, `failed` and reconciled entries after their retention period. It never
deletes `in_flight` or `uncertain` entries.

## Deadlines and cancellation

| Setting (`[switchboard]` / `switchboard`) | Range  | Default | Python | Native | Meaning                                       |
| ----------------------------------------- | ------ | ------- | ------ | ------ | --------------------------------------------- |
| `tool_timeout_seconds`                    | 5-3600 | 120     | yes    | yes    | Deadline for one tool execution               |
| `job_timeout_seconds`                     | 5-3600 | 300     | yes    | yes    | Deadline for one polling-job run              |
| `job_workers`                             | 1-16   | 4       | yes    | no     | Concurrent polling lanes in the Python runner |

An attempt's `deadline_at` is its start plus the applicable timeout. Engines apply the remaining time to web
requests made inside the call and fail them once it passes. Neither engine forcibly interrupts arbitrary
handler code: Python threads cannot be killed, and the native engine does not abandon a write midway. A
deadline that expires during a write produces an uncertain attempt, never an automatic retry.

Native cancellation (`Runtime::cancel`, `sb_runtime_cancel`) aborts web transfers of the call in progress and
does not affect later calls. A cancelled write is uncertain. The Python runner stops scheduling new jobs on
shutdown and lets running jobs finish or reach their deadlines.

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

`SB_EFFECT_CONFIRMED` means a software readback matched the requested state. It never proves a physical
effect. Adapters that read back state set a boolean `confirmed`; adapters that cannot tell whether a write
was delivered return `"effect_unknown": true` or raise the uncertain error.

## Storage, retention and backup

Each engine refuses state written by a newer schema instead of silently downgrading it. A backup is a
consistent online copy, and verification opens the copy read-only, checks database integrity, recognized
schema versions, and the audit chain, then reports the audit head. Verification does not prove that the
providers' or devices' current state matches the backup.

Python retention covers core tables (`[retention]`) and connectors that provide a retention hook
(`[retention.connectors]`). Native retention covers core tables. Neither purges open journal entries.

## Isolation

Approval separation is only meaningful with separate operating-system accounts. The broker owns the
configuration, credentials and database. The assistant's account reaches only the client socket, which
exposes the protocol's tool listing and calls. The operator's account reaches the operator socket, which
approves, denies, reconciles and toggles the kill switch. Each socket checks the peer's numeric user
identifier with the operating system's peer-credential facility, in addition to file permissions.

Running the assistant and the operator under one account restores the old boundary: that account can reach
both sockets. Neither engine claims a sandbox in that configuration.

### Operator socket protocol

Both brokers accept the same newline-delimited operator requests, so one operator client works with either
engine. A request is an object with `op`, an optional `request_id` (string or integer, echoed back), and only
the fields listed; unknown fields are refused.

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

A response is `{"ok": true, "op": ..., "request_id": ..., "result": ...}` or
`{"ok": false, "op": ..., "request_id": ..., "error": {"code": ..., "message": ...}}`. Review `approval`
before `approve`: approving executes exactly the stored arguments.

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

Runners compare instants and semantic outcomes, not storage formatting. Any further divergence a fixture
exposes must be added here with the fixture that demonstrates it.

## Fixtures and runners

[cases](./cases/) holds the shared fixtures: the `schema`, `policy`, `approvals`, `journal`, `exposure` and
`tasks` case files (format `switchboard-contract-cases`, `version` 1) and the synthetic tools both runners
register. Each case lists engine-neutral steps, such as `call`, `approve`, `reconcile` and `rpc`, with the
outcome they must produce; the [fixture reference](./cases/README.md) documents every key. A step on which the
engines legitimately differ carries `divergence` with a topic from the table above, a reason and per-engine
expectations. `approvals.json` and `tasks.json` also demonstrate the `Approval identifiers` and
`Stored timestamps` rows.

A case carries `known_violation` instead when an engine does not yet meet a normative rule. That runner reports
an expected failure and fails once the case passes, so the marker is removed when the engine is fixed. No case
carries one at present.

Run both runners on your own machine; these are unexecuted instructions:

```bash
uv run pytest -m contracts tests/contracts
cmake -S native -B build/native -DSWITCHBOARD_BUILD_TESTS=ON
cmake --build build/native --target native-test-contracts
ctest --test-dir build/native -L contracts --output-on-failure
```

The Python runner is [tests/contracts/test_contracts.py](../tests/contracts/test_contracts.py). The native
runner, [native/tests/contracts.cpp](../native/tests/contracts.cpp), reads the directory compiled in as
`SWITCHBOARD_CONTRACTS_DIR` and lists every failed case. No runner, engine, test or build was executed when
the fixtures and runners were written; only formatters, linters, type checks, a compile-only syntax check, and
JSON and Markdown checks ran.
