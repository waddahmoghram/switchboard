# Finance connector

The finance connector keeps a private ledger of your bank and card accounts on your own machine.
It is fed by statement files you download from online banking and by the transaction alert emails your bank already sends.
It does not use Plaid or any other paid aggregator, never asks for a bank password, and has **no tools that move money**: there is no SPEND-tier tool, no payment, and no transfer.

Configuration: [`docs/config/finance.toml`](../config/finance.toml).
Source:
[`src/switchboard/connectors/finance/`](../../src/switchboard/connectors/finance/).

## What it does

- Imports Open Financial Exchange (OFX) files in both forms (version 1.x in Standard Generalized Markup Language, SGML, and version 2.x in Extensible Markup Language, XML), Quicken's `.qfx` variant of OFX, and comma-separated values (CSV) exports, from an inbox directory you own.
- Turns bank alert emails, delivered as `mail.received` events by the mail connector, into *pending* transactions within minutes of a purchase, and links them to the posted transaction when the statement arrives, so each purchase counts once.
- Stores only the last four digits of account numbers and masks them everywhere as `****1234`.
- Summarizes income and spending by category per month, detects subscriptions and other repeating charges, and emits an event when a monthly budget is exceeded.

## Quick start

1. Create a private inbox directory:

   ```bash
   mkdir -m 700 ~/finance-inbox
   ```

2. Add a `[connectors.finance]` table to your configuration (start from [`docs/config/finance.toml`](../config/finance.toml)) and add `"finance"` to `connectors` in `[switchboard]`.
3. Check the setup:

   ```bash
   switchboard doctor
   ```

4. Save statement files into the inbox, then ask your assistant to run `finance_import_inbox`, or import one file yourself (the file is read in place, not moved):

   ```bash
   switchboard finance import ~/Downloads/statement.qfx
   switchboard finance summary --month 2026-11
   ```

## Getting your data from your bank

Menu names differ between banks and change over time, so these steps are generic.

**Statement files.**
Sign in to online banking, open the account's transaction history or statements area, and look for an option to download or export transactions.
Choose a date range and a format: OFX or QFX (often labeled for Quicken or Microsoft Money) gives the most reliable results, because each transaction carries a bank-assigned identifier; CSV works with a profile that describes the layout.
Save the file into your inbox.
Downloading every month or two, with ranges that overlap the previous download, is safe: duplicates are detected.

**Transaction alert emails.**
Many banks let you choose alerts in their security or notification settings, for example an email for every card purchase over a threshold, or for every deposit.
Ask for email alerts that include the amount and, if offered, the merchant and the last four digits of the card.
Then write an alert rule that recognizes the message (see [Bank alert emails](#bank-alert-emails)).

## Supported formats and their quirks

### OFX and QFX

The parser follows the OFX 2.3 specification published by Financial Data Exchange (FDX):

- **OFX 1.x** starts with plain header lines such as `OFXHEADER:100`, `DATA:OFXSGML`, and `CHARSET:1252` (Windows code page 1252), followed by SGML in which data elements usually have no end tag: `<TRNAMT>-80.32` ends where the next tag starts.
  Aggregates such as `<STMTTRN>` are closed.
- **OFX 2.x** starts with `<?xml ...?>` and `<?OFX OFXHEADER="200" ...?>` and closes every element.
- **QFX** is OFX plus Intuit-specific elements such as `<INTU.BID>`, which are ignored.
- Bank statements (`BANKMSGSRSV1/STMTTRNRS/STMTRS`) and credit card statements (`CREDITCARDMSGSRSV1/CCSTMTTRNRS/CCSTMTRS`) are read, including several accounts in one file.
  Each `STMTTRN` contributes `TRNTYPE`, `DTPOSTED`, `TRNAMT`, `FITID`, `NAME` (or `PAYEE`/`NAME`, `EXTDNAME`), `MEMO`, and `CHECKNUM`; `LEDGERBAL` and `AVAILBAL` become the account's balance.
- **Dates** look like `20261105120000.000[-8:PST]` and may be cut short (`20261105`).
  The ledger keeps the calendar date as the bank wrote it: converting to Coordinated Universal Time would move a 23:30 Pacific purchase to the next day, which is not the date on your statement.
- **Amounts** may use `.` or `,` as the decimal mark.
  `TRNAMT` is signed from your point of view: a card purchase or a checking debit is negative; a card payment is positive.
  An amount of a quadrillion or more is corrupt data and is refused like any unreadable amount.
- **Encodings** come from the XML declaration or from the `ENCODING` and `CHARSET` headers.
  Files that declare nothing, an encoding Python does not know, or a codec that is not a character set (`base64`, `zlib`, `punycode`) are decoded leniently as UTF-8.

A single tolerant tokenizer reads both syntaxes, so bank files never reach an XML parser: there are no document type definitions, entity expansion, or external resources to exploit, and the slightly malformed files some banks produce (an unescaped `&`, an unusual encoding label) still import.

Not supported: investment statements, pending transactions inside OFX (`BANKTRANLISTP`), corrections (`CORRECTFITID`), foreign-currency conversion details, and the older Quicken Interchange Format.

### CSV profiles

Every bank lays out its CSV export differently, so a `[connectors.finance.csv_profiles.<name>]` table names the columns and formats:

| Key                                 | Meaning                                                                                         |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| `date_column`, `description_column` | Header names (compared without regard to case). Required.                                       |
| `amount_column`                     | One signed amount column, **or** both `debit_column` and `credit_column`.                       |
| `debit_column`, `credit_column`     | Read as magnitudes, because some banks print debits as negative numbers and others as positive. |
| `date_format`                       | A `strptime` format or a list of them, tried in order. Must include a year, a month, and a day. |
| `invert_sign`                       | For exports that show purchases as positive numbers (common for cards).                         |
| `delimiter`, `decimal_mark`         | For example `";"` and `","` for `1.234,56`.                                                     |
| `encoding`                          | Default `utf-8-sig`, which skips a byte order mark; one is also ignored under plain `utf-8`.    |
| `skip_rows`                         | Rows of preamble before the header row.                                                         |
| `memo_column`, `id_column`          | Optional extra text and the bank's own transaction identifier, used for dedupe when present.    |
| `account`, `files`, `currency`      | Ledger account, a file-name glob such as `example-cu-*.csv` for the inbox, and the currency.    |

Amount cells may carry currency symbols, thousands separators, a trailing minus, or accounting parentheses: `$1,234.56`, `12.34-`, `(12.34)`.
Rows whose date or amount cannot be read (a "Total" footer) are skipped and counted; a file in which no row can be read fails, because that usually means the profile is wrong.
Files without a header row are not supported.

An inbox CSV file uses the profile whose `files` glob matches its name; if none matches, the only profile without a `files` glob is used.
On the command line, `--profile` chooses explicitly.

## Bank alert emails

Each `[[connectors.finance.alert_rules]]` entry recognizes one kind of alert.
The first rule that matches a message wins.

| Key           | Meaning                                                                                                |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| `from`        | Sender address glob, or a list of them, compared without regard to case. Required.                     |
| `subject`     | Optional regular expression that must be found in the subject.                                         |
| `body`        | Regular expression with named groups: `amount` (required), and optionally `merchant`, `last4`, `date`. |
| `direction`   | `"out"` (default: the amount is money spent, stored as negative) or `"in"` (a deposit).                |
| `account`     | Ledger account name; otherwise the account whose `last4` matches the `last4` group.                    |
| `date_format` | Formats for the `date` group; without a usable date, the email's `Date` header is used.                |
| `currency`    | Defaults to the connector's `currency`.                                                                |

Alerts are stored as pending transactions, deduplicated by Message-ID.
A date taken from the email's `Date` header is the calendar date in the connector's `timezone` setting (default: this computer's time zone), because the mail connector reports moments in Coordinated Universal Time and an evening purchase would otherwise land on the next day.
The same setting decides what "this month" means for summaries and budgets.
Keep regular expressions simple and anchored to literal phrases from the alert; they run on untrusted text.
Banks change alert wording without notice, so check `switchboard doctor` and the pending list after a bank redesigns its emails.

**Treat alert emails as untrusted.**
Anyone can send a message that looks like an alert, and sender addresses can be forged.
Alerts only ever create pending entries, never act on anything, and a later statement import is the source of truth.

## The ledger

The ledger is a set of `finance_` tables in Switchboard's SQLite database.

**Accounts.**
OFX files identify an account by its full number; the connector keeps only the last four characters and a salted SHA-256 (Secure Hash Algorithm) hash of the bank identifier and account number, so later imports find the same account.
The salt is random, created once, and stored in the database's key-value table.
The hash keeps account numbers out of the database; it is not encryption, because anyone who can read the database can also read the salt.
Map last digits to friendly names with `[connectors.finance.accounts.<name>]`; an account the configuration does not mention is named `account-1234`.
Naming it later keeps its history: the next OFX import or alert for those last four digits renames the one `account-1234` to the configured name, so alerts and statements stay in one account and a purchase is never counted twice.
(Two automatic accounts with the same last digits, or two different full numbers, stay apart.)
Accounts known only from CSV files are identified by name, so choose those names before importing.
Runs of nine or more digits in descriptions, memos, and file names are masked the same way (`****6789`).

**Dedupe.**
Importing the same file twice is a no-op (the file's SHA-256 is recorded).
Within an account:

- OFX and QFX transactions use the bank's financial institution transaction ID (`FITID`), which the specification requires to be unique within an account.
  It is stored salted and hashed, because some banks build these identifiers from account numbers.
- CSV rows use a hash of (account, date, amount, description, *occurrence*), where occurrence counts identical rows earlier in the same file.
  Two coffees for the same price on the same day are two real purchases with occurrences 0 and 1, so both are kept, and importing the same or an overlapping export again reproduces both keys, so neither is duplicated.
  The rare failure: a later export that contains only the second of two identical rows numbers it 0 and treats it as already imported.
- Alerts use the email's Message-ID.

**Pending and posted.**
An alert creates a pending transaction.
A posted transaction from a statement and an unmatched pending one are linked when they share an account and an amount and are at most three days apart; the most similar merchant wins, then the closest date.
Linking works in either arrival order, and a linked alert disappears from listings and totals.
If the posted amount differs from the alert (a restaurant tip, a fuel pre-authorization, currency conversion), the two are not linked and both count until you notice the pending entry.

**Categories, transfers, and budgets.**
`[connectors.finance.categories]` maps a category to a list of regular expressions, matched without regard to case against the normalized merchant, the description, and the memo; the first category in configuration order wins, otherwise `uncategorized`.
Categories apply when a transaction is stored; after editing them, run `switchboard finance recategorize`.
Categories listed in `transfer_categories` (default `["transfer"]`) are reported but left out of income and spending, so paying your card from checking does not count twice.
`[connectors.finance.budgets]` sets monthly limits per category; refunds in a category reduce its spending.

**Recurring charges.**
A merchant is reported when at least three charges, ending with its latest, repeat monthly (every 30 ± 5 days) or weekly (every 7 ± 2 days) with amounts within 10% of the latest.
Other purchases from the same merchant at different prices are skipped; a missed cycle or a much shorter interval ends the run.
A price change of more than 10% restarts detection.
The last 400 days are examined.

## Tools

| Tool                   | Tier  | What it does                                                              |
| ---------------------- | ----- | ------------------------------------------------------------------------- |
| `finance_accounts`     | READ  | Accounts with masked numbers and the newest statement balance (not live). |
| `finance_transactions` | READ  | Search by date, account, category, merchant, amount, and pending state.   |
| `finance_summary`      | READ  | Income, spending, and budgets by category for one month (`YYYY-MM`).      |
| `finance_recurring`    | READ  | Subscriptions with cadence, typical amount, last and next expected date.  |
| `finance_import_inbox` | DRAFT | Import new files from `import_dir` only and report counts per file.       |

`finance_transactions` takes `start`, `end`, `account`, `category`, `merchant_contains`, `min_amount`, `max_amount`, `pending`, and `limit` (at most 200).
Amount filters are signed: for purchases of 50 or more, use `max_amount: -50`.
`finance_summary` defaults to the current month.
`finance_import_inbox` never reads any other path.
Every schema bounds its inputs and rejects unknown properties.
There are no ACT or SPEND tools.

## Events

| Event                     | When                                               | Payload keys                                      |
| ------------------------- | -------------------------------------------------- | ------------------------------------------------- |
| `finance.transaction`     | A new transaction from an import or an alert       | See below                                         |
| `finance.budget_exceeded` | A category's spending this month passes its budget | `category`, `month`, `spent`, `limit`, `currency` |

`finance.transaction` payloads carry `id`, `account`, `date`, `amount`, `currency`, `merchant`,
`pending`, `source`, and `category`.

- `source` is `ofx`, `csv`, or `alert`; `pending` is true only for alerts no statement has confirmed.
- `amount` is a JavaScript Object Notation (JSON) number rounded to cents (`12.5` means 12.50).
  **Negative means money out**, positive means money in.
  Internally every amount is integer cents.
- One import (one `finance_import_inbox` call or one `switchboard finance import`) emits at most **20** `finance.transaction` events, for the newest new transactions; the result reports how many were held back.
  A first import of a year of history therefore does not flood your rules.
- A posted transaction that confirms an earlier pending alert emits nothing: the alert already did.
- `finance.budget_exceeded` fires at most once per category per month (recorded in the ledger, so event pruning cannot cause a repeat).
  Budgets are checked for the current month after each import and alert.

The connector subscribes to `mail.received` (from the mail connector).
It provides no services and uses none.

## Command line

```bash
switchboard finance import FILE [--account NAME] [--profile NAME]
switchboard finance summary [--month YYYY-MM]
switchboard finance recategorize
```

`import` reads any file you name (you typed the path) and leaves it in place; `--account` names the ledger account (for an OFX file, only when it holds one account).
`summary` runs through the policy gate like any other caller.

## Privacy and security

- The inbox must already exist, be a real directory (not a symbolic link), belong to you, and not be writable by other users; `switchboard doctor` also warns when others can read it.
  Only regular files with `.ofx`, `.qfx`, or `.csv` extensions are read, opened without following symbolic links, at most 20 MB each and 50 per call.
  Imported files move to `processed/` and unreadable ones to `failed/`.
  A file that hits an unexpected error (a locked database, say) is reported with status `error` and stays in the inbox for the next run; the files after it still import, and their events are still sent.
  The log names only the error type, never its message, which could quote the file.
- Error messages name rows, columns, and elements, never cell values or account numbers.
- Tool results contain masked numbers only.
  Transaction descriptions and merchants are visible to the assistant you connect; expose only the tools you need with `[mcp] expose`.
- The ledger lives in the Switchboard data directory, which is private (mode 700).
  Back it up like any other financial record.

## Tradeoffs and limits

- **No live balances.**
  Balances are as of the newest statement you imported; only an aggregator or a bank connection can provide live balances, and this connector deliberately uses neither.
- **File imports are manual or semi-automatic.**
  You download files yourself; the assistant or a person then imports the inbox.
- **Alert emails are bank-specific and change.**
  Each bank needs its own rule, and a redesigned email silently stops matching until you update the rule.
- **Matching needs exact amounts**, so tips and pre-authorizations leave a pending entry behind.
- **One currency per summary.**
  Transactions in other currencies are stored but not totaled.

## Paths not taken

- **Screen scraping bank websites.**
  It usually breaks the bank's terms of service, stalls on multi-factor authentication prompts, and requires storing your bank password where an assistant runs.
- **OFX Direct Connect** (desktop software downloading statements directly from a bank's OFX server).
  The GnuCash project's settings page now says Direct Connect "is no longer provided by many (any?) banks"; it also needs per-bank server details and a stored bank password.
  It could return as an explicit opt-in for the few banks that still offer it.
- **Paid aggregators** (Plaid, MX, Finicity, Teller).
  They are built for companies: Plaid's own pricing page asks you to request access to its pay-as-you-go plan.
  SimpleFIN Bridge, a read-only service, is inexpensive but still paid ($1.50 a month or $15 a year plus tax when checked).
  Pricing for MX, Finicity, and Teller was not verified.
- **The United States data-rights rule.**
  The Consumer Financial Protection Bureau (CFPB) finalized its Personal Financial Data Rights rule (section 1033 of the Dodd-Frank Act) in October 2024.
  A federal court stayed its compliance dates on October 29, 2025, and the CFPB began reconsidering the rule with an advance notice of proposed rulemaking on August 22, 2025.
  Its regulatory agenda published August 14, 2026 lists a rulemaking to reconsider the rule; a search of the Federal Register found no new proposed or final rule since then.
  Until that settles, bank data access depends on each bank's and aggregator's own terms, which is why this connector relies on files and emails you already receive.

## Verified facts (checked 2026-11-11)

- OFX 1.x headers (`OFXHEADER:100`, `DATA:OFXSGML`, `CHARSET`), the rule that OFX 1.0.2 parsers must accept but not require end tags, the date format `YYYYMMDDHHMMSS.XXX [gmt offset[:tz name]]`, amounts with `.` or `,` as the decimal mark, `FITID` uniqueness within an account, and `TRNAMT` signed from the customer's perspective ("a credit card payment is positive while a credit card purchase is negative"): [OFX Banking Specification v2.3](https://financialdataexchange.org/wp-content/uploads/2025/12/OFX-Banking-Specification-v2.3.pdf), sections 2.8, 3.2.3, 3.2.8, 3.2.9, and 11.4.4, and the OFX 1.0.2 example with `CHARSET:1252` in 16.5.4.
  The OFX work group now sits within [Financial Data Exchange](https://financialdataexchange.org/about-fdx/ofx-work-group/).
- QFX is Intuit's proprietary variant of OFX with additional fields:
  [Wikipedia, QFX](https://en.wikipedia.org/wiki/QFX_(file_format)) (a secondary source).
- Python's `utf-8-sig` codec skips a leading byte order mark when decoding:
  [codecs documentation](https://docs.python.org/3/library/codecs.html).
- CFPB rule status: [compliance page](https://www.consumerfinance.gov/compliance/compliance-resources/other-applicable-requirements/personal-financial-data-rights/) (stay on October 29, 2025; advance notice on August 22, 2025),
  [final rule](https://www.consumerfinance.gov/rules-policy/final-rules/required-rulemaking-on-personal-financial-data-rights/),
  [reconsideration notice](https://www.federalregister.gov/documents/2025/08/22/2025-16139/personal-financial-data-rights-reconsideration),
  and the [CFPB regulatory agenda of August 14, 2026](https://www.federalregister.gov/documents/2026/08/14/2026-16613/regulatory-agenda).
- OFX Direct Connect availability:
  [GnuCash wiki, OFX Direct Connect Bank Settings](https://wiki.gnucash.org/wiki/OFX_Direct_Connect_Bank_Settings).
- SimpleFIN Bridge pricing: [beta-bridge.simplefin.org](https://beta-bridge.simplefin.org/).
  Plaid plans: [plaid.com/pricing](https://plaid.com/pricing/).
