# Shopping connector

The shopping connector gives your assistants two read-mostly abilities without Zapier, paid
application programming interfaces (APIs), or retailer accounts:

* **Order and shipment tracking from your own mail.** It reads `mail.received` events from the mail
  connector, recognizes order confirmations and shipping or delivery notices, and keeps a small
  ledger: merchant, order id, total, carrier, tracking number, a public tracking link, and a status
  timeline.
* **Polite price watches.** You (or an assistant) save a product page and a target price. A
  background job fetches the page at most once per `min_fetch_interval_seconds` (six hours by
  default), honors `robots.txt` and `Retry-After`,
  reads the price from structured data, and raises `shopping.price_drop` when the price reaches your
  target.

It **never buys anything**. There is no purchase, cart, or checkout tool, by design; see
[Why there is no buy tool](#why-there-is-no-buy-tool).

Settings and suggested policy rules: [`docs/config/shopping.toml`](../config/shopping.toml).

## Tools

| Tool                    | Tier  | Default | What it does                                                                                      | What it will not do                        |
| ----------------------- | ----- | ------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `shopping_orders`       | read  | allow   | Orders found in mail: merchant, order id, total, currency, status. Filters: `status`, `merchant`. | Cancel, return, or change an order         |
| `shopping_shipments`    | read  | allow   | Shipments with carrier, tracking number, tracking link, and timeline. Filter: `status`.           | Contact a carrier                          |
| `shopping_watch_list`   | read  | allow   | Watches with target, last price, availability, last result, and next allowed check.               | Fetch anything                             |
| `shopping_watch_add`    | draft | allow   | Save an `https://` product page with `target_price`, `currency`, and an optional `note`.          | Fetch the page now, or ever buy            |
| `shopping_watch_remove` | draft | allow   | Stop a watch; its price history stays.                                                            | Delete history                             |
| `shopping_check_prices` | read  | allow   | Run due checks now and report each outcome.                                                       | Skip robots.txt, Retry-After, or intervals |

Every schema sets `additionalProperties: false` and bounds strings and numbers. `target_price`
accepts a JavaScript Object Notation (JSON) number or a string of plain digits such as `"49.99"`; it
is stored as `Decimal`, never as a float. Re-adding a Uniform Resource Locator (URL) updates its
target and reactivates it but does not reset its next allowed check, so it cannot be used to fetch a
page early.

## Events

| Direction  | Type                  | Payload                                                                                   | Dedupe                         |
| ---------- | --------------------- | ----------------------------------------------------------------------------------------- | ------------------------------ |
| Subscribes | `mail.received`       | From the mail connector; only `from`, `subject`, `text`, and `message_id` are read        | -                              |
| Emits      | `shopping.order`      | `merchant`, `order_id`, `total`, `currency`, `source_message_id`                          | Once per merchant and order id |
| Emits      | `shopping.shipment`   | `merchant`, `order_id`, `carrier`, `tracking_number`, `tracking_url`, `status`            | Once per shipment and status   |
| Emits      | `shopping.price_drop` | `watch_id`, `url`, `title`, `price`, `currency`, `target`, `previous`                     | Once per watch and price       |

Amounts are strings with at least two decimals (`"1299.00"`). `shopping.shipment` fires when a shipment
first appears with a shipping status or moves forward to `shipped`, `out_for_delivery`, or
`delivered`. The connector provides no services and uses none, and it does no network input or
output while registering.

## Order and shipment tracking

**Classification.** A merchant rule applies when its `from_contains` text appears in the From header.
Its subject regexes are tried in the order delivered, shipped, ordered. When no rule matches the
sender, or none of its subject regexes matches, generic subject phrases are used: "was delivered",
"out for delivery", "has shipped", "is on its way", "order confirmation", "thank you for your order",
and a few more. Either way, a message counts only if it also contains an order id (after a label such
as "Order number", "Order #", or "Order ID") or a tracking number with context, so a newsletter that
says "your savings are on the way" is ignored. Without a rule, the merchant is the sender's domain.

**Totals.** Only order confirmations carry totals. A rule's `total_regex` is tried first, then the
labels "order total", "grand total", "total charged", "amount charged", and finally "total"; "Subtotal"
never matches. The currency comes from the rule, then from a currency code or an unambiguous symbol
(€, £, ₹) next to the total, then from `default_currency`. A bare `$` is ambiguous and is not guessed.

**Tracking numbers.** A pattern alone is never enough; the word "tracking" or the carrier's name must
appear within 60 characters before or after the number. Digit-only formats need the carrier's name
itself, because bare 10- and 12-digit runs are usually phone or order numbers. The order id is never
reported as a tracking number.

| Carrier                              | Pattern                                                                 | Context required                          | Public link (pattern observed 2026-11-11; may change)                                             |
| ------------------------------------ | ----------------------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------- |
| United Parcel Service (UPS)          | `1Z` plus 16 letters or digits, also `1Z 999 AA1 01 2345 6784` grouping | "tracking" or "UPS"                       | `https://www.ups.com/track?tracknum=<number>`                                                     |
| United States Postal Service (USPS)  | 20 to 22 digits, contiguous or in groups of four                        | "tracking", "USPS", or "Postal Service"   | `https://tools.usps.com/go/TrackConfirmAction?tLabels=<number>`                                   |
| FedEx                                | exactly 12 or 15 digits                                                 | "FedEx"                                   | `https://www.fedex.com/fedextrack/?trknbr=<number>`                                               |
| DHL                                  | exactly 10 digits                                                       | "DHL"                                     | `https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id=<number>` |

**Check digits are not validated.** The carriers' check-digit algorithms could not be verified from
an authoritative public source when this connector was written, so a pattern plus context is the
whole test. Links are for you to open; Switchboard never fetches them.

**Timeline.** Statuses only move forward: `ordered` → `shipped` → `out_for_delivery` → `delivered`.
Each shipment records when it first reached each status. An order confirmation creates a placeholder
shipment in `ordered`; the first tracking number later seen for that order takes it over, so the
timeline continues. A late or repeated email fills a missing timestamp but never moves a parcel
backwards and never emits a second event.

## Price watches

For each due watch, at most 25 per run:

1. **`allowed_domains`**: when set, other hosts are skipped.
2. **Reserve**: the watch's next allowed check moves forward *before* fetching, so the background runner
   and an assistant calling `shopping_check_prices` never fetch the same page twice.
3. **`robots.txt`**: fetched through the shared Hypertext Transfer Protocol (HTTP) client and
   evaluated by Switchboard's own Request for Comments (RFC) 9309 matcher for the product token
   `switchboard` (the user agent is `switchboard/<version> …`, and a `User-agent: switchboard` group
   applies in any letter case). Groups for the token are combined, else the `*` groups apply. The
   longest matching rule wins, `Allow` wins a tie, `*` matches any characters, and a final `$` anchors
   the end. The answer is cached per site in the key-value store, so every watch on the site shares it.

   | `robots.txt` answer                                                     | Result                                               |
   | ----------------------------------------------------------------------- | ---------------------------------------------------- |
   | 2xx                                                                     | Rules apply; cached for 24 hours                     |
   | 401 or 403                                                              | Disallow everything; cached for 24 hours             |
   | Other 4xx (such as 404)                                                 | Allow everything; cached for 24 hours                |
   | 429 or 503                                                              | Skip the site until `Retry-After`, else for one hour |
   | Other 5xx, network error, refused redirect, or over 500 kibibytes (KiB) | Skip the site as unreachable for one hour            |

4. **Fetch** the page with the honest Switchboard user agent, no cookies, and a 2 megabyte cap.
   Redirects are followed one hop at a time, at most five, and only to URLs that pass the same vetting
   as a watch URL (see [Security notes](#security-notes)). A 429 or 503 answer pauses the whole site
   until `Retry-After` (seconds or an HTTP date, capped at 30 days), or for one hour without it; the
   watch's next check is the later of that and the regular interval.
5. **Extract** the price, in this order:
   * JSON for Linked Data (JSON-LD) in `<script type="application/ld+json">`: a `Product` (also inside
     `@graph` or a top-level list) and its `offers`, which may be one `Offer`, a list (the first offer
     with a usable price wins), or an `AggregateOffer` (its `lowPrice`). `priceSpecification` is
     understood. Products nested inside the main product, such as `isRelatedTo`, are ignored. A bare
     `Offer` counts only when the page has no `Product`. Malformed scripts are skipped.
   * Meta tags: `product:price:amount` with `product:price:currency`, then the older
     `og:price:amount` with `og:price:currency`. `og:price:standard_amount` (the pre-sale price) is
     ignored.
   * The visible page text is never searched for currency symbols.
6. **Record** the observation and alert. `shopping.price_drop` fires when the price is at or below the
   target **and** lower than the previous observation; the first observation counts. A page that
   reports a different currency from the watch is recorded but never alerts. Each watch alerts once
   per price.

Prices are parsed as `Decimal`: JSON-LD numbers never become floats, `"1,299.00"` reads as 1299.00,
and ambiguous text such as `"19,99"` or `"1.299,00"` is refused rather than guessed. Order totals in
mail follow the same rule: `Total: 19,99 €` leaves the total empty instead of reading it as 19.

## Why there is no buy tool

The design is **human in the loop**: the assistant tells you that a watched item reached your target
and gives you the link; you open it, check the item, seller, size, and total, and buy it yourself.
If you want to limit the damage of a mistake or a leaked number, pay with a merchant-locked or
single-use virtual card from your card issuer or a virtual-card service.

Why not let the assistant check out:

* **No free, safe, terms-compliant way exists for arbitrary stores.** Agent checkout protocols, such
  as the Agentic Commerce Protocol (ACP) and the Universal Commerce Protocol (UCP), work only where a
  store has integrated them, usually through its platform or payment provider; they are not a way for
  a self-hosted script to check out anywhere. Automating a store's own checkout pages risks breaking
  its terms of service. Even OpenAI moved checkout back to merchants' own sites, writing that the
  initial version of Instant Checkout "did not offer the level of flexibility that we aspire to
  provide".
* **Agents still buy the wrong things.** In Visa's agentic commerce benchmark of 22 models on 15
  shopping challenges, the overall pass rate was 57.2%, all three attempts at a scenario passed only
  34.2% of the time, and about 54% of failed runs still ended in a purchase.
* **Payment credentials are the worst thing to put next to untrusted input.** This connector reads
  mail and web pages that anyone can write. A stored card number or store login next to that content
  invites prompt injection to spend money. Keeping payment entirely outside Switchboard removes that
  path.

If you want automatic buying for a routine item, prefer a merchant's own feature with its
notifications and cancellation window. The research notes describe Amazon's Auto Buy for Prime
members, which orders automatically at a target price and allows cancellation within 24 hours. Set
such targets only for items you would buy without looking again.

## Tradeoffs and limits

* **Email formats change.** Generic heuristics are deliberately cautious and miss some messages; add a
  merchant rule for stores you use often. Multi-package orders are tracked per tracking number, but a
  delivery email without a tracking number marks every package of that order delivered.
* **`robots.txt` is a floor, not permission.** A site's terms of service may forbid automated access
  even where `robots.txt` allows it. Use `allowed_domains` to limit watches to sites whose terms you
  have read. The connector identifies itself honestly and fetches each page at most once per
  `min_fetch_interval_seconds` (at least one hour; six hours by default).
* **One `robots.txt` reading on every Python version.** Rules are evaluated by Switchboard's own
  RFC 9309 matcher, not `urllib.robotparser`, whose matching differs between branches: the 3.12 branch
  compares plain prefixes and uses the first matching rule, so a `Disallow: /products/*` there blocks
  nothing.
* **Redirects stay inside the vetted space.** A page or `robots.txt` that redirects to a host outside
  `allowed_domains` (a content delivery network, say) reports `fetch_failed` or
  `robots_unreachable`; add that host if you trust it. RFC 9309 asks crawlers to follow `robots.txt`
  redirects across hosts, so this is stricter than the standard. `robots.txt` is read for the
  watched URL's site only: when a page redirects to another host, that host's `robots.txt` is not
  consulted.
* **JavaScript-rendered prices are invisible.** Pages that fill in the price with JavaScript, show it
  only to signed-in users, or answer bots with a challenge page report `no_price`.
* **Variant pages** described as `ProductGroup` without their own offers fall back to meta tags.
* **Carrier APIs require developer accounts.** The USPS APIs need a USPS Business Account, a
  registered app, and a consumer key and secret. The connector therefore only builds public tracking
  links and never reports carrier scan status.
* **No `Crawl-delay`.** With at least an hour between fetches of a page, the connector does not read
  `Crawl-delay`; several watches on one site are fetched back to back in the same run, until the site
  answers 429 or 503, which pauses all of them.

## Security notes

* **Product pages and mail are untrusted input.** Nothing from them is executed, links in them are never
  followed, and only small validated fields are stored: order ids with a restricted character set,
  `Decimal` amounts, three-letter currency codes, tracking numbers, and our own tracking links. Subjects
  and bodies are never stored. Page titles are stripped of control and formatting characters and
  capped at 120 characters, but they remain text written by a stranger. Assistants should treat them
  as data, never as instructions.
* **Watch URLs and every redirect are vetted:** `https` only; no Internet Protocol (IP) addresses,
  single-label names, or local suffixes such as `.local`, `.lan`, `.internal`, or `.home.arpa`; no
  embedded user names or passwords; inside `allowed_domains` when it is set.
* **Only public addresses are fetched.** Before each request the host name is resolved, and the
  request is refused if any address is loopback, private, link-local, or otherwise not publicly
  routable, so a name such as `127.0.0.1.nip.io` cannot reach your machine or your network. The
  connection resolves the name once more, and a hostile Domain Name System (DNS) server could answer
  differently the second time; the HTTPS certificate must still match the watched name, which an
  internal service is very unlikely to present. Where only a proxy can resolve outside names, the
  local lookup fails and checks report `fetch_failed`.
* **No secrets.** The connector needs no keys and sends no cookies, so it sees signed-out prices.
* **Logs** name watch ids, never URLs, because product URLs can carry session or affiliate tokens.
* **Your own regexes** run on untrusted mail. Avoid nested repetition such as `(a+)+`, which can make
  matching very slow.
* **Kill switch.** `shopping_watch_add` and `shopping_watch_remove` are draft tier, so the kill switch
  blocks them. The read tools, including `shopping_check_prices`, still run, and so does the
  background price job, which is not a tool call; remove watches or disable the connector to stop it.
  Add `[policy.tools.shopping_check_prices] decision = "ask"` to approve each manual check.

## Verified facts (checked 2026-11-11)

* RFC 9309, the Robots Exclusion Protocol: 4xx means "unavailable" and allows access (2.3.1.3); 5xx
  and network errors mean "unreachable" and require complete disallow (2.3.1.4); a cached copy should
  not be used for more than 24 hours (2.4); parsers must handle at least 500 KiB (2.5).
  [rfc-editor.org/rfc/rfc9309](https://www.rfc-editor.org/rfc/rfc9309)
* `Retry-After` is either an HTTP date or a number of seconds (RFC 9110, section 10.2.3), and a 429
  answer may include it (RFC 6585, section 4).
  [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3),
  [RFC 6585](https://www.rfc-editor.org/rfc/rfc6585#section-4)
* schema.org `price`: use `.` as the decimal point and avoid thousands separators; put the currency in
  `priceCurrency`, not a symbol. `lowPrice` is "the lowest price of all offers available".
  [schema.org/price](https://schema.org/price), [schema.org/lowPrice](https://schema.org/lowPrice)
* Product meta tags `product:price:amount` and `product:price:currency`:
  [Meta catalog microdata tags](https://developers.facebook.com/docs/marketing-api/catalog/guides/microdata-tags/)
  and [Pinterest product pins](https://developers.pinterest.com/docs/web-features/product-rich-pins/),
  which also defines `og:price:standard_amount` as the non-sale price. `og:price:amount` is an older
  form still seen on some pages; the examples on both pages use `product:price:amount`.
* RFC 9309 matching: product tokens match without regard to case, matching groups are combined, and
  otherwise the `*` group applies (2.2.1); the most specific match has the most octets, an equivalent
  `allow` should win, rules before the first group should be ignored, and `/robots.txt` is implicitly
  allowed (2.2.2); `$` ends the pattern and `*` matches zero or more characters (2.2.3); crawlers
  should follow at least five redirects, even across hosts (2.3.1.2). Checked 2026-11-11.
* `urllib.robotparser` matching differs by branch, which is why Switchboard does not use it: compare
  [3.12](https://github.com/python/cpython/blob/3.12/Lib/urllib/robotparser.py) and
  [3.13](https://github.com/python/cpython/blob/3.13/Lib/urllib/robotparser.py).
* USPS APIs require a USPS Business Account, an app, and a consumer key and secret:
  [developers.usps.com/getting-started](https://developers.usps.com/getting-started).
* Visa's benchmark figures:
  [Visa press release](https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22936.html).
* OpenAI's Instant Checkout change:
  [Powering product discovery in ChatGPT](https://openai.com/index/powering-product-discovery-in-chatgpt/).

**Not verified here:**

* Carrier tracking-number formats and tracking-page URLs. The USPS tracking help page answered an
  automated fetch with status 403, and the UPS help and developer pages are rendered by script or
  refused; FedEx and DHL formats come from commonly published examples. Treat the formats and links in
  the table above as patterns observed 2026-11-11 that may change.
* Amazon Auto Buy, from the owner's research notes (pages inspected 2026-11-10). It was not fetched
  again because it is a retailer's site:
  [Auto Buy help](https://www.amazon.com/gp/help/customer/display.html?nodeId=TsaUdPSIWqy1tZhF09).
