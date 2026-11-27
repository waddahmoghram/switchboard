# C++ engine, C interface and Rust ownership

The native implementation is C++20.
C99 and Rust are first-class clients of one policy/storage/connector engine, not three unrelated ports.
They require the native shared library, not a Python interpreter.

The C application binary interface (ABI) is version 1, revision 2.
Its public contract is [switchboard.h](../include/switchboard/switchboard.h); the C++ implementation is [c_api.cpp](../src/c_api.cpp).
C++ callers can use [Runtime](../include/switchboard/native.hpp) directly.
These are source contracts; binary compatibility across compiler/platform/version combinations is untested.
The cross-engine rules for outcomes, the action journal and isolation are in the [contract](../../contracts/README.md).

## Versions

`sb_abi_version()` must equal `SB_ABI_VERSION` (1).
`sb_abi_revision()` reports additions within that version and must be at least the revision a client was compiled for.
Revision 2 adds typed outcomes, approval review, operator journal, kill switch, backup and retention functions, and cancellation; revision 1 functions and their ownership rules are unchanged.
A program built against revision 2 cannot use a revision 1 library: the new symbols are missing.

## Ownership

* Initialize a runtime output pointer to `NULL` and every `sb_buffer` to `{NULL, 0}` before passing it in.
* Successful creation transfers one opaque runtime handle to the caller.
  Destroy it exactly once.
* An operation's output must be empty.
  Reusing an unfreed buffer returns an explicit invalid-argument code,
  rather than silently leaking or replacing it.
* The library owns output allocation.
  Read `length` bytes, then use `sb_buffer_free`; never use a different allocator.
  Output also has a trailing zero byte for convenience, excluded from `length`.
* Freeing a buffer resets its pointer and length; freeing an already reset buffer is harmless.
* Input bytes are borrowed for the call and are not retained.
  Pointers/counts must designate valid memory.
* An `sb_outcome` is caller storage.
  The library overwrites all of it and allocates nothing for it.
* Do not use a destroyed/forged handle or race destruction with active calls, including cancellation.
  C cannot make invalid pointers safe.
  Do not build clients with incompatible packing or nonstandard calling conventions.

The complete [C client](../clients/c/main.c) demonstrates bounded configuration input, all return-code checks, result cleanup and runtime destruction.
The C interface is covered by [C interface cases](../tests/c_api.cpp) and [pure C cases](../tests/c_client.c).

## Requests, status and errors

Configuration and request bodies use JavaScript Object Notation (JSON).
Input is limited to 1 mebibyte (MiB) per call; output is bounded to 8 MiB.
Invalid encoding/JSON and duplicate keys are errors, not repaired content.

`sb_runtime_call` takes a request like:

```json
{"tool":"switchboard_status","arguments":{}}
```

`sb_origin` distinguishes assistant, rule and explicit operator calls.
Operator convenience is not authentication.
Only the approval methods execute a stored approval; callers cannot select an approval origin to skip the queue.

`sb_status` describes the interface operation.
`SB_OK` means a report was returned, **not** that the report says a request was approved, completed, delivered or physically verified.
Inspect the JSON `status`, `approval_id`, `action`, returned observation and any adapter-specific uncertainty flags, or use typed outcomes.
The C/Rust demonstration clients preserve this distinction; their zero exit status is not a physical-success receipt.

Errors can allocate a JSON error report; free it just like success output.
Allocation failure can return only the numeric status.
C++ exceptions never cross the C boundary.
Expected errors have bounded public messages; unexpected failures do not expose raw exception/credential contents.

C++ callers receive JSON operation outcomes, with expected state/transport/configuration failures also able to raise `switchboard::Error`.
Destruction does not throw.
Policy/schema checks, private state and audit recording remain in C++; callers do not implement weaker copies of the gate.

## Typed outcomes

`sb_runtime_call_outcome` takes the same request and origin as `sb_runtime_call`, returns the same complete report, and also decodes it into an `sb_outcome`:

| Field         | Content                                                                               |
| ------------- | ------------------------------------------------------------------------------------- |
| `status`      | `SB_OUTCOME_OK`, `_PENDING`, `_DENIED`, `_INVALID` or `_ERROR`: the JSON `status`     |
| `effect`      | What is known about an external effect, from the action journal (table below)         |
| `approval_id` | 32 lowercase hexadecimal characters and a terminating zero byte, or empty when absent |
| `action_id`   | The journal entry of an executed non-read call in the same form, or empty when absent |

The effect follows the [contract table](../../contracts/README.md#typed-outcomes-for-c-and-rust):

| Report                                                               | `effect`                |
| -------------------------------------------------------------------- | ----------------------- |
| Read tool, `pending`, `denied`, `invalid`, or error before execution | `SB_EFFECT_NONE`        |
| `action.state` is `uncertain`                                        | `SB_EFFECT_UNKNOWN`     |
| `action.state` is `failed`                                           | `SB_EFFECT_FAILED`      |
| `succeeded` and `data.confirmed` is `true`                           | `SB_EFFECT_CONFIRMED`   |
| `succeeded` and `data.confirmed` is `false`                          | `SB_EFFECT_UNCONFIRMED` |
| `succeeded` without a boolean `data.confirmed`                       | `SB_EFFECT_RETURNED`    |

The library checks the journal entry before the status.
A report without `action` has no effect whatever its status.
A write handler that rejects its arguments after the entry was recorded reports `invalid` with a `failed` entry, so its effect is `SB_EFFECT_FAILED`.
Contradictory journal data never upgrades an effect: a `succeeded` entry on a non-`ok` report, a `failed` entry on an `ok` report, an unrecognized state or a report that also declares `effect_unknown` maps to `SB_EFFECT_UNKNOWN`.
`SB_EFFECT_CONFIRMED` means a software readback matched the requested state.
It never proves a physical effect.

When the function itself fails, the outcome still says what is known:

| Failure                                                                 | `status`           | `effect`            | Identifiers |
| ----------------------------------------------------------------------- | ------------------ | ------------------- | ----------- |
| Rejected before the engine ran: arguments, JSON, origin, buffer, handle | `SB_OUTCOME_ERROR` | `SB_EFFECT_NONE`    | Empty       |
| The engine failed during the call                                       | `SB_OUTCOME_ERROR` | `SB_EFFECT_UNKNOWN` | Empty       |
| The engine answered, then the report could not be returned              | From the answer    | From the answer     | From it     |

A null `outcome` is an invalid argument.
After `SB_EFFECT_UNKNOWN`, inspect the journal with `sb_runtime_actions` before repeating anything; the duplicate guard refuses an identical write while an earlier one is in flight or uncertain.

## Operator functions

These calls serve the operator.
Any code that holds a runtime handle can make them, so in-process use is a convenience, not a boundary; approval separation needs separate accounts and the [native broker](./isolation.md).

| Function                 | Input                                                                    | Notes                                                         |
| ------------------------ | ------------------------------------------------------------------------ | ------------------------------------------------------------- |
| `sb_runtime_approval`    | Approval identifier                                                      | One approval with its exact stored arguments; decides nothing |
| `sb_runtime_actions`     | JSON filter `{"state"?, "limit"?}` or `{"id"}`; zero bytes for no filter | A filter that is not a JSON object is an invalid argument     |
| `sb_runtime_reconcile`   | Exactly `{"id", "resolution", "note"}`                                   | `applied` or `not_applied`; only `uncertain` entries change   |
| `sb_runtime_kill_switch` | `-1` reports, `0` turns off, `1` turns on                                | Blocks new non-read calls; other values are invalid           |
| `sb_runtime_backup`      | Path of a new or empty private directory, created if absent              | Writes a consistent copy and a manifest                       |
| `sb_runtime_purge`       | `1` for a dry run, `0` to delete                                         | Never removes `in_flight` or `uncertain` entries              |
| `sb_verify_backup`       | Path of a backup directory; no runtime                                   | Read-only; reports `"verified": false` for a failed check     |

Paths are bounded UTF-8 (Unicode Transformation Format, 8-bit) text without zero bytes.
Field values are checked by the engine: an unknown state, a malformed or unknown identifier, a short note, a nonempty backup directory or a missing backup is a report whose `status` is `invalid` or `error`, returned with `SB_OK`.
Review an approval with `sb_runtime_approval` before approving it: approving executes exactly the stored arguments.
Reconciling records a human decision; it never repeats, reverses or verifies the effect.
A verified backup does not prove that providers or devices still match it.

## Cancellation

`sb_runtime_cancel` may be called from any thread while another thread runs a call on the same runtime.
It is lock-free, allocates nothing, never fails, and does nothing for a null handle or when no call runs.
It aborts web transfers of the call in progress, which then fails; a cancelled write is uncertain.
Later calls are not affected.
Arbitrary handler code is not interrupted.
Destruction must still wait until every other thread has stopped using the handle.

## Clients

Both demonstration clients take `CONFIG.json COMMAND [ARGUMENTS]`, or `verify-backup DIRECTORY` alone, which reads no configuration.
Their exit status reflects the interface status only.

| Command                                    | Function                                                   |
| ------------------------------------------ | ---------------------------------------------------------- |
| `catalog`, `tick`                          | Catalog and one runner tick                                |
| `call JSON`, `rpc JSON`                    | Assistant-origin call and one protocol message             |
| `outcome JSON`                             | Typed call; prints `{"outcome": {...}, "report": {...}}`   |
| `approval ID`, `approve ID`, `deny ID`     | Approval review, then the decision                         |
| `actions [FILTER_JSON]`                    | Journal listing or one entry                               |
| `reconcile ID applied\|not_applied NOTE`   | Reconciliation; the C client escapes the note as JSON text |
| `kill-switch status\|on\|off`              | Kill switch                                                |
| `backup DIRECTORY`, `purge dry-run\|apply` | Backup and retention                                       |

## Rust

The [safe Rust wrapper](../clients/rust/src/lib.rs) checks the interface version and revision, owns each handle and response buffer, validates returned UTF-8, and releases native allocations on success or error.
Foreign pointers and unsafe operations are private implementation details.

| Item                                           | Purpose                                                                |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| `Runtime::call_outcome(request, origin)`       | Returns `Outcome { status, effect, approval_id, action_id, report }`   |
| `Status`, `Effect`                             | Typed mirrors of the C constants, with `as_str()` and `Display`        |
| `Runtime::approval(id)`                        | One approval with its stored arguments, before `approve` or `deny`     |
| `Runtime::actions(filter)`                     | Journal listing; an empty string means no filter                       |
| `Runtime::reconcile(id, Resolution, note)`     | `Resolution::Applied` or `Resolution::NotApplied`; the note is escaped |
| `Runtime::kill_switch(KillSwitch)`             | `KillSwitch::Report`, `Off` or `On`                                    |
| `Runtime::backup(path)`, `Runtime::purge(dry)` | Backup directory and retention                                         |
| `verify_backup(path)`                          | Free function; needs no runtime                                        |
| `Runtime::cancel_handle()`                     | `CancelHandle<'_>`, which is `Send`, `Sync` and `Copy`                 |

`Runtime` deliberately does not implement `Send` or `Sync`.
Use it on its owning thread; do not assume threaded destruction or custom callback semantics.
A `CancelHandle` borrows its runtime, so the runtime cannot be dropped while a handle exists, and its only operation is the thread-safe, lock-free cancellation above.
Use it with `std::thread::scope`:

```rust
let cancel = runtime.cancel_handle();
std::thread::scope(|scope| {
    scope.spawn(move || cancel.cancel());
    let outcome = runtime.call_outcome(request, Origin::Assistant);
});
```

A Rust `Ok` means a native report was returned, not that the contained application outcome is successful.
`Err` after a typed call started can hide an effect, as in C. Paths must be valid UTF-8 (`Error::InvalidPath`).

Rust does not have a second JSON/policy/parser dependency stack.
Application callers may choose their own JSON library to inspect returned reports, but must not ignore pending, denied, error or unconfirmed states.
The [Rust client](../clients/rust/src/main.rs), [lifecycle cases](../clients/rust/tests/lifecycle.rs) and [outcome cases](../clients/rust/tests/outcomes.rs) show the public wrapper.

## Validation boundary and limitations

[Validation](../../docs/VALIDATION.md#current-status) records where the C, C++ and Rust tests named here have run.
Passing tests do not prove migration of real data, conformance with real protocol clients, or compatibility with providers.

The existing Python implementation remains independent and fuller in scope.
Native configuration, state,
approval identifiers and hashes are not Python interchange formats.
See the [native coverage table](../README.md#coverage) before switching a client or assuming language-level feature parity.
