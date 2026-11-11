"""CardDAV (RFC 6352) reading: discover address books, list their cards, and download them in batches.

Cards are listed with a ``Depth: 1`` PROPFIND and downloaded with ``addressbook-multiget`` (§8.7),
which every CardDAV server must support, sent with ``Depth: 0`` as that section requires.
"""

from __future__ import annotations

from collections.abc import Sequence

from switchboard.connectors._dav import (
    CARDDAV,
    GETETAG,
    RESOURCETYPE,
    XMLNS,
    DavClient,
    Flavor,
    collection_url,
    discover,
    href_element,
    qname,
)

ADDRESS_DATA = qname(CARDDAV, "address-data")
FLAVOR = Flavor(
    well_known="/.well-known/carddav",
    home_set=qname(CARDDAV, "addressbook-home-set"),
    collection_type=qname(CARDDAV, "addressbook"),
    noun="address book",
)
BATCH = 100
_MULTIGET = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<CR:addressbook-multiget {xmlns}><D:prop><D:getetag/><CR:address-data/></D:prop>{hrefs}"
    "</CR:addressbook-multiget>"
)


def multiget_body(urls: Sequence[str]) -> bytes:
    return _MULTIGET.format(xmlns=XMLNS, hrefs="".join(href_element(url) for url in urls)).encode()


def card_urls(client: DavClient, book: str) -> list[str]:
    """Members of an address book that are not collections themselves."""
    members = client.propfind(collection_url(book), [RESOURCETYPE, GETETAG], depth="1")
    own = book.rstrip("/")
    return [member.href for member in members if not member.types() and member.href.rstrip("/") != own]


def fetch_cards(client: DavClient) -> list[str]:
    """The vCard text of every card in every address book behind the configured URL."""
    texts: list[str] = []
    for book in discover(client, FLAVOR):
        urls = card_urls(client, book.href)
        for index in range(0, len(urls), BATCH):
            body = multiget_body(urls[index : index + BATCH])
            for resource in client.report(collection_url(book.href), body, depth="0"):
                text = resource.text(ADDRESS_DATA)
                if text:
                    texts.append(text)
    return texts
