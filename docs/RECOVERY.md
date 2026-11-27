# Recovery and bounded execution

This guide is for the operator: the person who owns the configuration, approves actions, and reads the audit log.
It explains how Switchboard's Python engine records actions that change things outside it, what to do when such an action has an unknown outcome, how long tool calls and polling jobs may run, and how the background runner shares work between threads and stops.
The cross-engine rules are in the [contract](../contracts/README.md); this page describes the Python engine.

[Validation](./VALIDATION.md#current-status) records where the tests for this behaviour have run.
Nothing on this page was exercised against a real provider, account, or device, and an action recorded as succeeded is not proof of a physical effect.

## The action journal

Every tool call that is not a read is recorded in the `action_journal` table of the database in your data directory *before* its handler runs.
That covers the draft and act tiers, and a spend-tier tool in the rare case that your policy explicitly lets one run (spend tools are denied by default, and none ships).
Read tools are not journaled because repeating a read changes nothing.

| State                    | Meaning                                                                     |
| ------------------------ | --------------------------------------------------------------------------- |
| `in_flight`              | Recorded, and the handler is running or its process ended mid-call          |
| `succeeded`              | The handler returned a report that does not declare its effect unknown      |
| `failed`                 | The handler stated that the effect did not happen                           |
| `uncertain`              | The effect may or may not have happened                                     |
| `reconciled_applied`     | You checked an uncertain action and recorded that its effect happened       |
| `reconciled_not_applied` | You checked an uncertain action and recorded that its effect did not happen |

An attempt is classified from how its handler finished:

1. It returned a JavaScript Object Notation (JSON) object with a top-level `"effect_unknown": true`:
   **uncertain**.
2. It returned anything else: **succeeded**.
   A connector's own confirmation stays in the report.
3. It raised `UncertainOutcomeError`: **uncertain**.
4. It raised any other `ToolError`: **failed**.
   Raising it asserts that nothing was sent, or that the provider or device refused the request.
5. It raised anything else, including a web transport error that a connector did not handle: **uncertain**.
6. Its process ended mid-call: the attempt stays `in_flight` until a *sweep* marks it **uncertain**, once its deadline is more than 60 seconds in the past.
   Sweeps run before every new attempt, whenever a command or the assistant server opens the application, and on every runner tick.

Executed non-read calls return their journal entry in the outcome, for example:

```json
{
  "status": "ok",
  "tool": "wled_set_power",
  "data": {"status": "error", "confirmed": false, "effect_unknown": true, "...": "..."},
  "message": "the outcome is unknown (action 3f0c...); an identical request is refused until ...",
  "action": {"id": "3f0c9a1e7b2d4c6f8a0b1c2d3e4f5a6b", "state": "uncertain"}
}
```

`status` says how the call went (`ok` means the handler returned a report), while `action.state` says what is known about the effect.
Read calls, pending approvals, and calls refused before execution have no `action`.
Assistants connected through the Model Context Protocol (MCP) see the same object as `structuredContent`.

Two report fields matter for devices.
`confirmed` is true when the connector read the device's state back and it matched the request; it is a software readback, not proof of a physical change.
`effect_unknown` is true when the connector cannot tell whether the request was delivered.
These connectors report or raise unknown outcomes:

| Connector                 | Unknown when                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------- |
| Telegram replies          | Transport failure, server error, unreadable reply, or mismatched confirmation      |
| LinkedIn publishing       | Any outcome other than a confirmed post or an explicit refusal                     |
| WLED controls             | The write failed or its readback did not confirm the requested state               |
| Shelly plug control       | The device did not clearly accept or reject the command                            |
| Home Assistant controls   | Transport failure, a server error, or a request timeout on the action              |
| Home Assistant generic    | The same, raised as an error by `home_call_service`                                |
| Home Assistant speech     | The same, for `home_announce`; playback is never verified either way               |
| Mail send                 | The connection dropped or timed out during submission, after sign-in               |
| Mail draft                | The connection failed during `APPEND` to the Drafts mailbox                        |
| Notifications             | Any channel lost its connection, got HTTP 408 or 5xx, or got an unreadable reply   |
| Calendar event creation   | The `PUT` lost its connection, or the server answered HTTP 408 or 5xx              |

Server refusals, such as an SMTP rejection or an HTTP 4xx other than 408, stay definite failures.
A notification is uncertain only for the channels listed under `uncertain` in its result; when another channel delivered it, the call still succeeds as a report but its action is `uncertain`.

## Why the duplicate guard exists

When an action's outcome is unknown, repeating it blindly could send a second message, publish a second post, or apply a second change.
So before recording a new attempt, Switchboard looks for an earlier attempt of the same tool with the same arguments (compared as canonical JSON, so key order does not matter) that is still `in_flight` or `uncertain`.
If one exists, the call is refused with status `denied` and a message naming the blocking action, and the handler does not run.

* Approved requests are checked when they execute.
  An approval that meets the guard finishes as `failed` with the same message, and `approvals_status` shows no action for it.
* Only identical requests are refused.
  A request with different arguments runs normally.
* Rules are covered too.
  A rule that keeps sending the same arguments is refused after one attempt with an unknown outcome until you reconcile it.
* The guard is lifted by reconciliation, in either direction, or by the owner process recording a late result (below).
  It never expires on its own.

## Reconciliation workflow

Only you, the operator, reconcile: with the commands below, or through the broker's operator socket when you run one.
There is no tool for it, so an assistant cannot clear the guard.

1. Find open actions.
   `switchboard doctor` prints how many are uncertain or in flight, assistants can call the read-only `actions_recent` tool, and you can list them:

   ```bash
   switchboard actions list --state uncertain
   ```

   Each line shows the action identifier, state, tool, origin, start time, and the recorded problem.
2. Inspect one.
   `show` prints the stored arguments, which can contain private message text:

   ```bash
   switchboard actions show 3f0c9a1e7b2d4c6f8a0b1c2d3e4f5a6b
   ```

3. Check outside Switchboard whether the effect happened: the sent folder, the chat, your profile, the device's own app, or the provider's history.
4. Record what you found, with a note of 1 to 500 printable characters:

   ```bash
   switchboard actions resolve 3f0c9a1e7b2d4c6f8a0b1c2d3e4f5a6b \
     --applied --note "Reply visible in the Telegram chat at 14:05"
   ```

   or

   ```bash
   switchboard actions resolve 3f0c9a1e7b2d4c6f8a0b1c2d3e4f5a6b \
     --not-applied --note "Nothing in the profile; safe to post again"
   ```

   The command shows the action and its arguments and asks you to confirm.
   Without a terminal it refuses unless you pass `--yes`.
   It writes an `action.reconciled` record to the audit log.
5. Repeat the request yourself if it did not happen and you still want it.

Reconciling records your decision.
It never repeats, reverses, or checks the effect.
Only `uncertain` actions can be reconciled; an `in_flight` one becomes uncertain after its deadline plus 60 seconds, and the resolve command sweeps first, so an attempt from a process that has stopped can be settled then.

The process that started an attempt may still record its own late result after a sweep marked it uncertain (for example, a slow handler that eventually returns), until you reconcile it.
No other process can.
Retention never deletes `in_flight` or `uncertain` entries.

## Deadlines

| Setting (`[switchboard]`) | Range  | Default | Meaning                                                     |
| ------------------------- | ------ | ------- | ----------------------------------------------------------- |
| `tool_timeout_seconds`    | 5-3600 | 120     | Deadline for one tool execution; also its longest lane wait |
| `job_timeout_seconds`     | 5-3600 | 300     | Deadline for one polling-job run, including its lane wait   |
| `job_workers`             | 1-16   | 4       | Polling jobs that may run at once in the runner             |

A tool's journal entry has `deadline_at` equal to its start plus `tool_timeout_seconds`.
Inside a tool execution or a job run, the deadline applies to web requests made through Switchboard's Hypertext Transfer Protocol (HTTP) client:

* each request's socket timeout is shortened to the time left;
* no request starts once the deadline has passed;
* a response still arriving after the deadline fails, checked between receive calls;
* for public-only requests (shopping price checks), the address lookup stops waiting at the request's timeout.

What is **not** bounded:

* Name resolution for ordinary requests.
  The Domain Name System (DNS) lookup goes to the operating system's resolver, which applies its own timeouts.
* A single receive call, which can wait up to the request's own timeout before the deadline is checked again.
* Mail.
  The Internet Message Access Protocol (IMAP) and Simple Mail Transfer Protocol (SMTP) libraries use their own per-socket timeouts and do not read the deadline.
* Any other handler or job code: file parsing, database waits, and computation.
  Python threads cannot be killed, so such code keeps running past its deadline.

A deadline that expires during a write produces a transport failure, which the connectors above report as an unknown outcome.
Writes are never retried automatically.

### Retries for reads

Repeating a request is opt-in, and only for `GET` and `HEAD`.
A caller asks for 0 to 3 retries.
Repeated are network failures that might pass (refused or reset connections, timeouts, an interrupted response, a lookup that did not finish) and the statuses 408, 429, 502, 503, and 504.
Each retry waits a jittered backoff (0.25-0.5, 0.5-1, then 1-2 seconds) or the server's `Retry-After` when that is at most 30 seconds.
No wait is made that would leave less than one second of the deadline; the last response or error is returned instead.
Asking to retry any other method is a programming error.

Today only the calendar feed download uses retries (two of them).

## The runner

`switchboard run` polls connectors on a small pool of worker threads and processes events on its main thread.

* **Lanes.**
  A job's lane is its name up to the first dot, which is its connector's name: `mail.poll` runs in lane `mail`.
  Jobs in one lane never overlap.
  Tool executions hold the lane of their connector, so a tool that a rule triggers never runs while its own connector's job is running in the same process.
* **Workers.**
  At most `job_workers` jobs run at once.
  A due job waits for a free worker and a free lane.
* **One run per job.**
  A job runs at a fixed interval from the start of its previous run.
  If it is still running when the next run is due, that run is skipped and counted as an overrun.
* **Stuck jobs.**
  A run still going after `job_timeout_seconds` is reported as stuck in a warning log.
  It keeps its worker and its lane until it returns, and its job is not started again until then.
  A tool that needs the lane of a stuck job gives up about a second after that job's deadline instead of waiting out its own timeout.
* **Events stay single-threaded.**
  Events, rules, and the tools that rules call run only on the main thread,
  between waits.
  A finishing job wakes the main thread early.

`switchboard run --once` runs every job once, waits for them within their deadlines, processes events, and exits.
It exits with status 1 and names the problem when a job failed, is stuck, or could not start because its lane or every worker stayed held by a stuck run:

```text
ran 3 job(s); processed 2 event(s)
failed jobs: shopping.prices
stuck jobs (still running past their deadline): mail.poll
jobs not started (their lane or every worker stayed busy): mail.prune
```

Here `calendar.refresh` ran cleanly, `mail.poll` passed its deadline and kept the `mail` lane, so `mail.prune` never started.
The first line goes to standard output and the others to standard error.

The long-running `switchboard run` stops on the interrupt signal (SIGINT, Ctrl+C) or the termination signal (SIGTERM).
The first signal stops scheduling new jobs and waits for running jobs to finish or reach their deadlines; a second signal exits at once.
It exits with status 1, naming the jobs, when it had to leave stuck jobs running.
Pending events stay queued for the next start.

## Limits

* The journal lives in the database, so every process that opens it (runner, assistant server, command line) shares the duplicate guard.
  Lanes are per process: the runner and a separately started assistant server do not exclude each other.
* A lane covers one connector's own code.
  A tool that uses another connector's service, such as a calendar invitation or an email notification sent through the mail connector, holds only its own lane.
* Job status (runs, failures, overruns, stuck) is kept in the runner's memory and reported by its logs and by `run --once`; another process cannot read it.
* Classification depends on connectors raising the right error.
  Connectors not listed above make no external writes, or report their expected failures with ordinary errors, which the journal records as `failed`.
  Calendar invitations and email notifications pass on the mail connector's unknown outcome.
* A sweep can only infer an interruption from the clock.
  If the machine's clock jumps forward, attempts may be marked uncertain early; reconcile them as usual.
