"""Unit tests for contact search and the contacts connector's tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.app import App
from switchboard.connectors.contacts.service import search
from switchboard.errors import ConfigError
from switchboard.policy import Origin, Tier

AppFactory = Callable[..., App]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
CARDS: list[dict[str, Any]] = [
    {
        "source": "book",
        "uid": "1",
        "fn": "Dr. Alex J. Rivera",
        "org": "Example Labs, Research",
        "emails": [{"value": "alex.rivera@example.invalid", "types": ["work"]}],
        "phones": [{"value": "+1 555-0100", "types": ["cell"]}],
    },
    {
        "source": "book",
        "uid": "2",
        "fn": "Sam Example",
        "org": "Example Co",
        "emails": [{"value": "sam@example.invalid", "types": []}],
        "phones": [{"value": "+44-20-7946-0000", "types": []}],
    },
    {"source": "book", "uid": "3", "fn": "Alexandra Stone", "org": "", "emails": [], "phones": []},
]


def _names(query: str, limit: int = 25) -> list[str]:
    results, _ = search(CARDS, query, limit)
    return [result["name"] for result in results]


def test_name_matches_rank_prefixes_first() -> None:
    assert _names("alex") == ["Alexandra Stone", "Dr. Alex J. Rivera"]


def test_every_word_must_match_somewhere() -> None:
    assert _names("example labs") == ["Dr. Alex J. Rivera"]
    assert _names("rivera research") == ["Dr. Alex J. Rivera"]
    assert _names("rivera stone") == []


def test_email_and_phone_queries() -> None:
    assert _names("sam@example.invalid") == ["Sam Example"]
    assert _names("555-0100") == ["Dr. Alex J. Rivera"]
    assert _names("+44 20 7946") == ["Sam Example"]
    assert _names("12") == []


def test_results_are_minimal_and_limited() -> None:
    results, total = search(CARDS, "example", 1)
    assert total == 2
    assert results == [
        {
            "name": "Sam Example",
            "emails": [{"address": "sam@example.invalid", "types": []}],
            "phones": [{"number": "+44-20-7946-0000", "types": []}],
            "source": "book",
            "org": "Example Co",
        }
    ]


def _config(path: Path) -> dict[str, Any]:
    return {
        "switchboard": {"connectors": ["contacts"]},
        "connectors": {"contacts": {"sources": {"phone": {"type": "vcf_file", "path": str(path)}}}},
    }


def test_tools_are_read_only_and_search_reads_the_vcf_file(make_app: AppFactory) -> None:
    app = make_app(_config(FIXTURES / "contacts.vcf"))
    assert {tool.name: tool.tier for tool in app.registry.all() if tool.connector == "contacts"} == {
        "contacts_sources": Tier.READ,
        "contacts_search": Tier.READ,
    }
    assert "contacts.refresh" in [job.name for job in app.ctx.scheduler.jobs()]

    outcome = app.call("contacts_search", {"query": "rivera"}, Origin.ASSISTANT)

    assert outcome.status == "ok", outcome.message
    assert outcome.data["untrusted_content"] is True
    [alex] = outcome.data["results"]
    assert alex["name"] == "Dr. Alex J. Rivera"
    assert [email["address"] for email in alex["emails"]] == [
        "alex.rivera@example.invalid",
        "alex@home.example.invalid",
    ]
    assert "note" not in alex
    assert "bday" not in alex
    [source] = app.call("contacts_sources", {}, Origin.ASSISTANT).data["sources"]
    assert (source["name"], source["type"], source["cards"], source["error"]) == (
        "phone",
        "vcf_file",
        3,
        None,
    )


def test_a_missing_file_is_reported_as_a_warning(make_app: AppFactory, tmp_path: Path) -> None:
    app = make_app(_config(tmp_path / "missing.vcf"))
    outcome = app.call("contacts_search", {"query": "anyone"}, Origin.ASSISTANT)
    assert outcome.status == "ok"
    assert outcome.data["results"] == []
    [warning] = outcome.data["warnings"]
    assert "cannot read the file" in warning


def test_limit_is_bounded_by_the_schema(make_app: AppFactory) -> None:
    app = make_app(_config(FIXTURES / "contacts.vcf"))
    assert app.call("contacts_search", {"query": "a", "limit": 26}, Origin.ASSISTANT).status == "invalid"
    assert app.call("contacts_search", {"query": "a", "extra": 1}, Origin.ASSISTANT).status == "invalid"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            {"type": "carddav", "url": "https://dav.example.invalid/", "username": "o", "password": "plain"},
            "secret",
        ),
        ({"type": "ldap", "url": "ldaps://dav.example.invalid/"}, "type must be one of"),
    ],
)
def test_invalid_sources_are_rejected_at_startup(
    make_app: AppFactory, source: dict[str, Any], message: str
) -> None:
    config = {
        "switchboard": {"connectors": ["contacts"]},
        "connectors": {"contacts": {"sources": {"bad": source}}},
    }
    with pytest.raises(ConfigError, match=message):
        make_app(config)
