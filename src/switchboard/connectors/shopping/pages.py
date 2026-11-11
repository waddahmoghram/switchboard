"""Extract a product price from an HTML page without running it or trusting it.

Sources, in order:

1. JSON-LD (``<script type="application/ld+json">``): a ``Product`` with ``offers`` (one Offer, a list,
   or an ``AggregateOffer`` using ``lowPrice``), found at the top level, in lists, or in ``@graph``.
   When no Product is present, a bare Offer or AggregateOffer is used. Malformed scripts are skipped.
2. Product meta tags: ``product:price:amount`` with ``product:price:currency``, then the older
   ``og:price:amount`` with ``og:price:currency``.

The visible page text is never searched for currency symbols: that guesses wrong on pages full of
related products, shipping thresholds, and "was" prices. Prices rendered only by JavaScript are
invisible to this plain fetch.
"""

from __future__ import annotations

import json
import unicodedata
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from switchboard.connectors.shopping.money import normalize_currency, parse_price

MAX_SCRIPTS = 20
MAX_OFFERS = 20
MAX_TITLE = 120
_MAX_DEPTH = 12
_MAX_STEPS = 20_000

PRODUCT_TYPES = frozenset({"Product", "ProductGroup", "IndividualProduct", "ProductModel", "SomeProducts"})
OFFER_TYPES = frozenset({"Offer", "AggregateOffer"})
_META_KEYS = frozenset(
    {
        "product:price:amount",
        "product:price:currency",
        "og:price:amount",
        "og:price:currency",
        "product:availability",
        "og:availability",
        "og:title",
    }
)
_META_PRICE_PAIRS = (
    ("product:price:amount", "product:price:currency"),
    ("og:price:amount", "og:price:currency"),
)


@dataclass(frozen=True)
class PriceFound:
    price: Decimal
    currency: str | None
    availability: str | None
    title: str | None
    source: str  # "json-ld" or "meta"


def extract_price(html: str) -> PriceFound | None:
    """The product's price from structured data, or None when the page does not state one."""
    page = scan_page(html)
    fallback_title = clean_text(page.meta.get("og:title"), MAX_TITLE) or clean_text(page.title, MAX_TITLE)
    return _from_json_ld(page.json_ld, fallback_title) or _from_meta(page.meta, fallback_title)


@dataclass
class PageScan:
    json_ld: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    title: str = ""


class _Scanner(HTMLParser):
    """Collects JSON-LD script bodies, the first value of each interesting meta tag, and the title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = PageScan()
        self._script: list[str] | None = None
        self._title: list[str] | None = None
        self._title_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and _is_json_ld(values.get("type")) and len(self.result.json_ld) < MAX_SCRIPTS:
            self._script = []
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content")
            if key in _META_KEYS and content is not None and key not in self.result.meta:
                self.result.meta[key] = content.strip()
        elif tag == "title" and not self._title_done:
            self._title = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.result.json_ld.append("".join(self._script))
            self._script = None
        elif tag == "title" and self._title is not None:
            self.result.title = "".join(self._title)
            self._title = None
            self._title_done = True

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self._title is not None:
            self._title.append(data)


def scan_page(html: str) -> PageScan:
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner.result


def _is_json_ld(kind: str | None) -> bool:
    return kind is not None and kind.split(";")[0].strip().lower() == "application/ld+json"


def _from_json_ld(scripts: list[str], fallback_title: str | None) -> PriceFound | None:
    """Products win; bare offers count only when the page declares no Product at all."""
    candidates = [node for script in scripts for node in _candidates(_load_json(script))]
    products = [node for node in candidates if _types(node) & PRODUCT_TYPES]
    if products:
        return _first_found(_from_product(node, fallback_title) for node in products)
    return _first_found(_from_offer(node, fallback_title) for node in candidates)


def _first_found(results: Iterable[PriceFound | None]) -> PriceFound | None:
    return next((found for found in results if found is not None), None)


def _load_json(text: str) -> Any:
    """Parsed JSON with Decimal numbers, or None for malformed input. NaN and Infinity become None."""
    try:
        return json.loads(_strip_wrappers(text), parse_float=Decimal, parse_constant=lambda _name: None)
    except (ValueError, RecursionError):
        return None


def _strip_wrappers(text: str) -> str:
    cleaned = text.strip()
    for opener, closer in (("<!--", "-->"), ("<![CDATA[", "]]>")):
        if cleaned.startswith(opener) and cleaned.endswith(closer):
            cleaned = cleaned[len(opener) : -len(closer)].strip()
    return cleaned


def _candidates(document: Any) -> list[dict[str, Any]]:
    """Product and offer objects, breadth first.

    The walk does not descend into a Product or Offer, so related products nested inside the main
    product (``isRelatedTo``, ``isSimilarTo``) are never mistaken for it.
    """
    found: list[dict[str, Any]] = []
    queue: deque[tuple[Any, int]] = deque([(document, 0)])
    steps = 0
    while queue and steps < _MAX_STEPS:
        steps += 1
        value, depth = queue.popleft()
        if depth > _MAX_DEPTH:
            continue
        if isinstance(value, list):
            queue.extend((item, depth + 1) for item in value)
        elif isinstance(value, dict):
            if _types(value) & (PRODUCT_TYPES | OFFER_TYPES):
                found.append(value)
            else:
                queue.extend((item, depth + 1) for item in value.values())
    return found


def _types(node: Mapping[str, Any]) -> set[str]:
    """Local type names: "Product", "schema:Product", and "https://schema.org/Product" all give "Product"."""
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {_local_name(item) for item in values if isinstance(item, str)}


def _local_name(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _from_product(product: Mapping[str, Any], fallback_title: str | None) -> PriceFound | None:
    """The first offer of the product that states a usable price."""
    title = clean_text(product.get("name"), MAX_TITLE) or fallback_title
    offers = product.get("offers")
    for offer in (offers if isinstance(offers, list) else [offers])[:MAX_OFFERS]:
        if isinstance(offer, dict):
            found = _from_offer(offer, title)
            if found is not None:
                return found
    return None


def _from_offer(offer: Mapping[str, Any], title: str | None) -> PriceFound | None:
    amount = offer.get("lowPrice") if "AggregateOffer" in _types(offer) else None
    if amount is None:
        amount = offer.get("price")
    specification = _first_mapping(offer.get("priceSpecification"))
    if amount is None and specification is not None:
        amount = specification.get("price")
    price = parse_price(amount)
    if price is None:
        return None
    currency = normalize_currency(offer.get("priceCurrency"))
    if currency is None and specification is not None:
        currency = normalize_currency(specification.get("priceCurrency"))
    return PriceFound(price, currency, _availability(offer.get("availability")), title, "json-ld")


def _first_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, Mapping) else None


def _availability(value: Any) -> str | None:
    """Turn "https://schema.org/InStock" into "InStock"; anything that is not text is dropped."""
    if not isinstance(value, str):
        return None
    return clean_text(_local_name(value.strip()), 40)


def _from_meta(meta: Mapping[str, str], title: str | None) -> PriceFound | None:
    for amount_key, currency_key in _META_PRICE_PAIRS:
        price = parse_price(meta.get(amount_key))
        if price is not None:
            availability = meta.get("product:availability") or meta.get("og:availability")
            currency = normalize_currency(meta.get(currency_key))
            return PriceFound(price, currency, clean_text(availability, 40), title, "meta")
    return None


def clean_text(value: Any, limit: int) -> str | None:
    """Untrusted text made safe to store and show: no control or format characters, single spaces, capped."""
    if not isinstance(value, str):
        return None
    visible = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    text = " ".join(visible.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or None
