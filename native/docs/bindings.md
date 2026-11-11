# C++ engine, C interface and Rust ownership

The native implementation is C++20. C99 and Rust are first-class clients of one policy/storage/connector
engine, not three unrelated ports. They require the native shared library, not a Python interpreter.

The C application binary interface (ABI) is version 1. Its public contract is
[switchboard.h](../include/switchboard/switchboard.h); the C++ implementation is
[c_api.cpp](../src/c_api.cpp). C++ callers can use [Runtime](../include/switchboard/native.hpp) directly.
These are source contracts; binary compatibility across compiler/platform/version combinations is untested.

## Ownership

* Initialize a runtime output pointer to `NULL` and every `sb_buffer` to `{NULL, 0}` before passing it in.
* Successful creation transfers one opaque runtime handle to the caller. Destroy it exactly once.
* An operation's output must be empty. Reusing an unfreed buffer returns an explicit invalid-argument code,
  rather than silently leaking or replacing it.
* The library owns output allocation. Read `length` bytes, then use `sb_buffer_free`; never use a different
  allocator. Output also has a trailing zero byte for convenience, excluded from `length`.
* Freeing a buffer resets its pointer and length; freeing an already reset buffer is harmless.
* Input bytes are borrowed for the call and are not retained. Pointers/counts must designate valid memory.
* Do not use a destroyed/forged handle or race destruction with active calls. C cannot make invalid pointers
  safe. Do not build clients with incompatible packing or nonstandard calling conventions.

The complete [C client](../clients/c/main.c) demonstrates bounded configuration input, all return-code checks,
result cleanup and runtime destruction. [C interface cases](../tests/c_api.cpp) and
[pure C cases](../tests/c_client.c) are authored, not executed.

## Requests, status and errors

Configuration and request bodies use JavaScript Object Notation (JSON). Input is limited to 1 MiB per call;
output is bounded to 8 MiB. Invalid encoding/JSON and duplicate keys are errors, not repaired content.

`sb_runtime_call` takes a request like:

```json
{"tool":"switchboard_status","arguments":{}}
```

`sb_origin` distinguishes assistant, rule and explicit operator calls. Operator convenience is not
authentication. Only the approval methods execute a stored approval; callers cannot select an approval
origin to skip the queue.

`sb_status` describes the interface operation. `SB_OK` means a report was returned, **not** that the report
says a request was approved, completed, delivered or physically verified. Inspect the JSON `status`,
`approval_id`, returned observation and any adapter-specific uncertainty flags.
The C/Rust demonstration clients preserve this distinction; their zero exit status is not a physical-success receipt.

Errors can allocate a JSON error report; free it just like success output. Allocation failure can return
only the numeric status. C++ exceptions never cross the C boundary. Expected errors have bounded public
messages; unexpected failures do not expose raw exception/credential contents.

C++ callers receive JSON operation outcomes, with expected state/transport/configuration failures also able
to raise `switchboard::Error`. Destruction does not throw. Policy/schema checks, private state and audit
recording remain in C++; callers do not implement weaker copies of the gate.

## Rust

The [safe Rust wrapper](../clients/rust/src/lib.rs) checks the interface version, owns each handle and
response buffer, validates returned UTF-8, and releases native allocations on success or error.
Foreign pointers and unsafe operations are private implementation details.

The wrapper deliberately does not implement `Send` or `Sync`. Use it on its owning thread; do not assume
threaded destruction or custom callback semantics. A Rust `Ok(String)` means a native JSON report was returned,
not that the contained application outcome is successful.

Rust does not have a second JSON/policy/parser dependency stack. Application callers may choose their own
JSON library to inspect returned reports, but must not ignore pending, denied, error or unconfirmed states.
The [Rust client](../clients/rust/src/main.rs) and [lifecycle cases](../clients/rust/tests/lifecycle.rs) show
the public wrapper. Tests are authored, not collected or run on the authoring host.

## Validation boundary and limitations

Compiler syntax/type checks of C/C++ and metadata-only checks of the Rust library/client do not execute the
application, tests or examples. They do not prove successful linking, correct allocation under failure,
runtime behavior, migrations, protocol conformance or provider compatibility. Rust test bodies are authored;
they were not collected or run.

The existing Python implementation remains independent and fuller in scope. Native configuration, state,
approval identifiers and hashes are not Python interchange formats. See the [native coverage table](../README.md#coverage)
before switching a client or assuming language-level feature parity.
