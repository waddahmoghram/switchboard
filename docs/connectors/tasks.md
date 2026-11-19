# Local tasks and reminders

This connector keeps personal tasks in Switchboard's private database.
It needs no external task provider or subscription.
Reminders are one-shot events for the existing rule runner, **not an automatic email, phone notification, or proof that a task was performed**.

The code and synthetic tests are authored, not executed.
Nothing here was run on the authoring host.
Follow [the test instructions](../DEPLOYMENT.md#run-the-tests) on your own machine before use.

## Tools

| Tool           | Tier  | Behavior                                                                               |
| -------------- | ----- | -------------------------------------------------------------------------------------- |
| `tasks_status` | read  | Counts and reminder-job error state                                                    |
| `tasks_list`   | read  | Literal search, list/status/overdue filters, bounded notes previews, identifier paging |
| `tasks_read`   | read  | One task with full bounded notes and its current revision                              |
| `tasks_add`    | draft | Create a local task with optional due and reminder date-times                          |
| `tasks_update` | draft | Edit the exact current revision, including completed/cancelled/open status             |
| `tasks_delete` | act   | Permanently remove the exact task revision; approval by default                        |

Titles are limited to 200 characters, notes to 4,000, and list names to 64.
Search returns at most 100 tasks per call and a `next_after_id` cursor.
Notes in list results are capped at 180 characters; use `tasks_read` for full notes.
Returned task content is untrusted data.

Updates and deletions require both `id` and `revision`.
A changed revision is a visible error, not permission to overwrite a newer edit.
Deletion approved before a later edit cannot delete that newer task.
Task identifiers are not reused.

## Setup on your own machine

Merge [the configuration](../config/tasks.toml) and add `tasks` to the existing connector list.
Keep unrelated connector names.
If you restrict assistant exposure, deliberately add the task tools you want to `[mcp] expose`.

These are unexecuted examples; substitute the actual identifier and revision from each result:

```bash
uv run switchboard call tasks_add \
  --args '{"title":"Review the example notebook","list_name":"personal","due_at":"2026-11-12T10:00:00-08:00","remind_at":"2026-11-12T09:30:00-08:00"}'
uv run switchboard call tasks_list
uv run switchboard call tasks_read --args '{"id":1}'
uv run switchboard call tasks_update --args '{"id":1,"revision":1,"status":"completed"}'
```

Due and reminder date-times must include an explicit numeric timezone offset or `Z`.
Ambiguous local times such as `2026-11-12T09:30:00` are rejected rather than guessed.
Stored instants are normalized to a zero offset with second precision.
Set a date field to `null` to clear it.
There are no recurring schedules, natural-language date parsing, or calendar/task-provider synchronization.

Local creation and edits are draft-tier operations.
Assistant or rule requests to delete a task ask for approval by default.
As elsewhere in Switchboard, an explicit human-origin command-line action acts as the operator's approval; [that is not a same-user sandbox](../DEPLOYMENT.md#the-operating-boundary).

## Reminders and notifications

`switchboard run` checks reminders every 60 seconds by default; the interval must be 30–3,600 seconds.
At most `max_reminders_per_tick` due reminders are queued per check (1–500, default 100).
A stopped or failed runner cannot deliver on time.
Other slow jobs can also delay a check; the interval is not a maximum delivery delay.
An overdue backlog drains across later checks.
`tasks_status.last_check` is the last stored check, shared between runner and assistant processes; `null` means no recorded check.
Its timestamp is not proof the runner is still alive.
`job_error` additionally describes the current process's scheduler, not another process's live state.
If the database itself cannot be written, even failure metadata may remain stale.

The job emits `tasks.reminder_due`, with `id`, `revision`, `title`, `due_at`, `remind_at`, `untrusted_content`, and `delivery = "event_queued"`.
A task's `reminder_event_queued_at` and `reminder_delivery` mean exactly that: an event was queued, **not that a person received it**.
The event and its marker commit together.

To deliver through a personal channel, enable and configure [notifications](./notify.md), then merge a rule such as this into your existing rules:

```toml
[[rules]]
name = "Personal task reminder"
on = "tasks.reminder_due"
[[rules.do]]
tool = "notify_self"
args = { title = "Task reminder", message = "{payload.title} (task {payload.id})" }
```

Notification transport, its policy, credentials, rate limits, and possible failure still apply.
Rules are not a durable delivery workflow and failed delivery is not automatically retried.
Inspect the [event and audit records](../DEPLOYMENT.md#failures-retries-and-retained-data).

Editing a title, changing a due date, or reopening a completed task does not resend an already queued reminder.
Changing `remind_at` to a different instant creates a new reminder; clearing it cancels future queueing.
Completed and cancelled tasks do not queue new reminders.

**Already queued events cannot be withdrawn.**
Completion, cancellation, deletion, or rescheduling after queueing does not remove that event.
A delayed notification can therefore describe an older task revision.
Inspect the current task before taking further action.
There is no exactly-once external notification guarantee.

## Architecture and retained data

The `tasks_items` table stores task fields, revisions, and reminder state.
All multi-statement changes use a database transaction.
Identifier-only `tasks.changed` and `tasks.deleted` events commit with their mutations; `tasks.reminder_due` additionally carries the bounded title so rules can render it.
The internal `tasks.local` service is trusted application code, not another policy boundary.

Deletion removes the task row, not audit records, queued events, backups, or copies already returned to an assistant.
Notes can be sent to a hosted model when exposed; self-hosting this store does not make the model local.
There is no general automatic task-retention policy.

Authored regression cases are in [unit tests](../../tests/unit/test_tasks.py) and [integration flows](../../tests/integration/test_tasks_flow.py).
Static checks do not prove scheduler timing, migration behavior, concurrency, notification delivery, or runtime compatibility.
