# Security policy

Switchboard is a personal prototype; read the status note in the [README](./README.md) before relying on any control described here.

## Reporting a vulnerability

Please report a suspected vulnerability privately, not in a public issue, pull request, or discussion.

Use GitHub's private vulnerability reporting: open the repository's security tab (**Security and quality** in GitHub's current interface) and choose **Report a vulnerability**, as GitHub's guide to [privately reporting a security vulnerability](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately) describes.
That button exists only after the repository owner enables private vulnerability reporting for this repository.
If it is missing, open an issue that asks for a private contact and contains no details of the vulnerability.

A useful report includes:

- the commit you tested;
- the component: the Python package, the native engine, the C or Rust client, the broker, a connector, or a GitHub Actions workflow;
- the configuration needed to reproduce it, with secrets removed;
- the steps or a proof of concept, and the impact.

Use your own accounts, devices, and synthetic data: never put real credentials or personal data in a report, and never test against accounts or devices that you do not own.
This is a personal project without a response-time commitment; please allow time for a fix on `master` before disclosing a problem publicly.

## Supported versions

| Version                                          | Security fixes |
| ------------------------------------------------ | -------------- |
| Latest commit on `master`                        | Yes            |
| Older commits, other branches, forks, and copies | No             |

There are no maintained releases: fixes land on `master` only, and nothing is backported.

## Trust model in brief

- Assistants are untrusted callers.
  Every tool call goes through one dispatcher: argument validation, policy, audit, then execution.
  Read and draft tools normally run immediately, act tools normally ask for approval, and no payment or checkout tool is implemented.
- The policy gate controls calls through Switchboard, not other programs running as your user.
  By default everything runs under your account, so an assistant with shell or file access there can approve its own requests, remove the kill-switch file, read credentials, or bypass Switchboard entirely.
- The optional broker moves the configuration, credentials, database, and audit log to a dedicated service account and gives the assistant and the operator separate local sockets, so that approval separation rests on operating-system accounts.
  The administrator and the service account, with everything they run, and the operator account remain trusted.
- Self-hosting removes the aggregator, not the model provider: a hosted assistant's provider can receive tool results, including mail and financial data.
- The audit log is tamper-evident, not tamper-proof: anyone who can rewrite the whole file can recompute its hash chain, and removing a valid tail cannot be detected without an independently stored head hash.

The details are in [the operating boundary](./docs/DEPLOYMENT.md#the-operating-boundary), [Isolation](./docs/ISOLATION.md) and its [native counterpart](./native/docs/isolation.md), and [trust boundaries and threats](./docs/ARCHITECTURE.md#trust-boundaries-and-threats).

## In scope

Ways to defeat a control within that trust model, for example:

- a tool call that runs without the validation, policy decision, approval, or audit record that the documentation describes, or with other arguments than the ones approved;
- an assistant calling a tool outside the exposure list;
- with the broker and separate accounts, the assistant's account reading the configuration, secrets, database, or audit log, using the operator socket, or deciding its own approvals;
- a secret that the configuration references reaching logs, audit records, tool results, or other files, or data files created with broader permissions than documented;
- a watched page reaching an internal address, or a forged or replayed webhook being accepted;
- a change inside the audit log that verification accepts although the records after it were not rewritten;
- an identical repeat of a write whose outcome is `uncertain` running before you reconcile it;
- memory-safety or undefined-behavior defects in the native engine or its C interface that tool arguments, protocol messages, or provider responses can reach;
- a way for pull request code to get write access or secrets in the GitHub Actions workflows.

## Out of scope

- Attacks that start with code already running as your account in the default single-account deployment.
- With the broker, attacks that start as the administrator, the service account, or the operator account; a broker started with `--allow-shared-account` provides no separation.
- An operator approving a harmful request: approval executes exactly the stored arguments.
- Data that allowed tools return reaching the assistant or its model provider, and personal data in audit records, approval arguments, or results: redaction covers credential keys, not content.
- Rewriting the whole audit log, or removing its tail.
- Resource exhaustion that the broker's one-operation-at-a-time design allows, such as a busy assistant delaying the operator until the running call's deadline.
- Defects in providers, devices, the model provider, the operating system, the Python interpreter, or third-party libraries; report those upstream, unless Switchboard uses them unsafely.
- Older commits, forks, and modified copies.
