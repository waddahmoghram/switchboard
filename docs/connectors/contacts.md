# Contacts connector

The contacts connector lets assistants look up people in your own address books: names,
organizations, email addresses, and phone numbers.
It reads two kinds of sources:

* **`carddav`**: a server using Web Distributed Authoring and Versioning (WebDAV) with vCard Extensions to WebDAV (CardDAV), defined in Request for Comments (RFC) 6352, such as iCloud, Fastmail, Nextcloud, or Radicale.
* **`vcf_file`**: a `.vcf` export on this machine, for example from your phone.

Cards are downloaded on a schedule and cached locally, so searches are fast and do not touch the network.
The connector never changes an address book.

## Tools

| Name               | Tier | Purpose                                                                                |
| ------------------ | ---- | -------------------------------------------------------------------------------------- |
| `contacts_sources` | read | List sources with their type, last refresh, card count, and last error (no addresses)  |
| `contacts_search`  | read | Find contacts by name, organization, email address, or phone number                    |

`contacts_search` takes `query` (1–100 characters) and `limit` (default 10, maximum 25).
Every word of the query must appear in the name, organization, or an email address; a query that looks like a phone number (at least three digits) also matches numbers by their digits, so `555 0100` finds `+1 (555) 0100`.
Names that start with the query come first.
Each result has only `name`, `emails` (address and types such as `work` or `pref`), `phones` (number and types), `org` when present, and `source`, with at most five addresses and five numbers.
Results carry `"untrusted_content": true`, plus `total` and `truncated`.

## Events, jobs, and services

* **Job `contacts.refresh`** runs every `refresh_interval_seconds` (default 21,600, six hours).
  It downloads every source and replaces that source's cached cards in one transaction.
  One failing source never stops the others.
* `contacts_search` also refreshes a source on demand when it was not checked within the interval,
  and reports failures as warnings while still searching the cached cards.
* Emits no events, subscribes to none, and provides and uses no services.

## Setup

Add `"contacts"` to `[switchboard] connectors`, copy the parts you need from [docs/config/contacts.toml](../config/contacts.toml), and run `switchboard doctor`.
It checks every secret reference and file path without printing values, and reports the last refresh error of each source.

### Provider notes (checked 2026-11-11)

* **Google Contacts**: Google's CardDAV interface accepts only OAuth 2.0 (the authorization framework of RFC 6749); Google "does not support any other authentication method" ([Google CardDAV](https://developers.google.com/people/carddav)).
  Switchboard does not implement OAuth.
  Instead, export your contacts from Google Contacts (More actions → **Export**, which offers a vCard file; [Google Contacts Help](https://support.google.com/contacts/answer/7199294)) and use a `vcf_file` source.
  Google's CardDAV page notes that Google uses vCard 3.0, which this connector reads.
* **iCloud**: use an app-specific password.
  Apple requires two-factor authentication for them; create one at account.apple.com under **Sign-In and Security → App-Specific Passwords** ([Apple Support 102654](https://support.apple.com/en-us/102654)).
  Apple's page does not list server names; the vdirsyncer project, which is regularly tested against iCloud, uses `https://contacts.icloud.com/` ([vdirsyncer iCloud notes](https://vdirsyncer.pimutils.org/en/stable/tutorials/icloud.html)).
  Discovery then moves to a recognized per-account host such as `p42-contacts.icloud.com`, which Switchboard explicitly permits over the standard encrypted port.
* **Fastmail**: CardDAV server `https://carddav.fastmail.com/` with an app password; your regular password does not work, and Basic plans do not include CardDAV.
  Without discovery, the personal address book is `https://carddav.fastmail.com/dav/addressbooks/user/<you@domain>/Default` ([Fastmail, "Server names and ports"](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports)).
* **Self-hosted servers**: point `url` at the address book or the server root.
  Plain `http://` is accepted for `localhost` and, with `allow_http = true`, for a server on your own network.

## How it works

* **Discovery** follows RFC 6764: the configured address may be an address book, a principal, or a server root; otherwise `/.well-known/carddav` leads to `current-user-principal`, then `addressbook-home-set`, then the address books.
* **Download**: a `PROPFIND` with `Depth: 1` lists the cards in each address book, and an `addressbook-multiget` REPORT (RFC 6352 §8.7, which every CardDAV server must support) fetches them in batches of 100, sent with `Depth: 0` as that section requires.
  A `429` or `503` reply whose `Retry-After` gives a delay in seconds pauses that source until then (at most a day).
* **Parsing** follows RFC 6350 (vCard 4.0) and tolerates vCard 3.0 (RFC 2426): folded lines,
  quoted parameters, `TYPE` lists, `PREF`, Apple's `item1.EMAIL` groups, `tel:` values, and text escapes.
  It reads `FN` (or builds a name from `N`), `EMAIL`, `TEL`, `ORG`, `BDAY`, `NOTE` (cut to 500 characters), and `UID`.
* **Cache**: the `contacts_cards` table keeps `source`, `uid`, `fn`, the email addresses and phone numbers as JavaScript Object Notation (JSON), `org`, and `updated_at`.
  Birthdays and notes are parsed but never stored or returned.
  Refresh times and errors live in the core key-value table.

## Tradeoffs and limits

* **Polling, not push.**
  Changes appear after the next refresh, every six hours by default.
  WebDAV sync tokens (RFC 6578) and entity tags are not used, so each refresh downloads every card again; that is fine for personal address books of a few thousand cards.
* **Search is simple.**
  Matching is case-insensitive substring matching, not fuzzy matching, and it does not ignore accents.
* **vCard 2.1** quoted-printable values are not decoded, and structured addresses (`ADR`) are not read.
* Contacts with neither a name nor an email address nor a phone number are skipped.

## Security notes

* Passwords are secret references resolved only when a refresh runs; error messages name the source, never the credentials.
* Credentials go only to the configured origin or recognized iCloud account hosts over Hypertext Transfer Protocol Secure (HTTPS) on port 443.
  Other hosts and changed ports are refused, including arbitrary siblings under the same parent domain.
  Every redirect is checked before credentials are sent.
* Server replies that declare a `DOCTYPE` or entities are refused; the parser never fetches external entities.
* Contact text is untrusted data, so results are flagged `untrusted_content`.
  Only minimal fields are returned, which limits what a manipulated assistant could copy elsewhere.
* `vcf_file` paths come only from your configuration; assistants cannot choose files.

## Configuration reference

| Key                         | Default | Meaning                                                    |
| --------------------------- | ------- | ---------------------------------------------------------- |
| `refresh_interval_seconds`  | `21600` | Refresh period for every source (300 to 2,592,000)         |
| `sources.<name>.type`       | —       | `carddav` or `vcf_file`                                    |
| `sources.<name>.url`        | —       | `carddav`: address book or server address                  |
| `sources.<name>.username`   | —       | `carddav` user name                                        |
| `sources.<name>.password`   | —       | `carddav` secret reference (an app-specific password)      |
| `sources.<name>.path`       | —       | `vcf_file` path                                            |
| `sources.<name>.allow_http` | `false` | Allow `http://` for a server on your own network           |

Source names may use letters, digits, `.`, `_`, and `-`.

## Tests

```bash
pytest tests/unit -k "contacts or dav"
pytest -m integration tests/integration/test_contacts_dav_server.py
```

The integration test starts a fake CardDAV server on `127.0.0.1` with an ephemeral port and walks the whole discovery chain, including the `/.well-known/carddav` redirect.
