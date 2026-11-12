"""Immutable content, exact claim binding, and durable single-attempt state."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from switchboard.connectors.linkedin.drafts import MIGRATIONS, DraftStore, PublishState
from switchboard.errors import ToolError, UncertainOutcomeError
from tests.conftest import AppFactory

AUTHOR = "urn:li:person:SYNTHETIC"


@pytest.fixture
def store(make_app: AppFactory) -> DraftStore:
    app = make_app()
    app.ctx.db.add_migrations("linkedin", MIGRATIONS)
    app.ctx.db.migrate()
    return DraftStore(app.ctx)


def _draft(store: DraftStore, text: str = "Exact synthetic text.\n") -> dict[str, Any]:
    return store.create(text, "PUBLIC", AUTHOR, "202610")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE linkedin_drafts SET text = ? WHERE id = ?",
        "UPDATE linkedin_drafts SET visibility = ? WHERE id = ?",
        "UPDATE linkedin_drafts SET author_urn = ? WHERE id = ?",
        "UPDATE linkedin_drafts SET api_version = ? WHERE id = ?",
        "UPDATE linkedin_drafts SET draft_hash = ? WHERE id = ?",
        "UPDATE linkedin_drafts SET id = ? WHERE id = ?",
    ],
)
def test_stored_draft_content_cannot_be_updated(store: DraftStore, statement: str) -> None:
    draft = _draft(store)
    with store.ctx.db.session() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(statement, ("changed", draft["draft_id"]))
    assert store.read(draft["draft_id"])["publish_args"] == draft["publish_args"]


def test_offline_drafts_without_an_actor_remain_local_only(store: DraftStore) -> None:
    draft = store.create("Local only.", "CONNECTIONS", "", "202610")
    assert draft["publish_args"] is None
    assert draft["state"] == "DRAFT"
    assert store.list(1)["drafts"][0]["preview_only"] is True


def test_draft_hash_covers_actor_visibility_version_text_and_unique_identifier(store: DraftStore) -> None:
    first = _draft(store)
    second = _draft(store)
    assert first["draft_id"] != second["draft_id"]
    assert first["draft_hash"] != second["draft_hash"]
    assert first["text"] == "Exact synthetic text.\n"
    assert first["publish_args"]["text"] == first["text"]
    assert len(first["draft_hash"]) == 64


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("text", "Changed text"),
        ("visibility", "CONNECTIONS"),
        ("author_urn", "urn:li:person:OTHER"),
        ("api_version", "202609"),
        ("draft_hash", "0" * 64),
    ],
)
def test_claim_refuses_changed_approved_fields(store: DraftStore, key: str, value: str) -> None:
    draft = _draft(store)
    changed = {**draft["publish_args"], key: value}
    with pytest.raises(ToolError, match="exactly match"):
        store.claim(changed, author_urn=AUTHOR, api_version="202610")
    assert store.read(draft["draft_id"])["state"] == "DRAFT"


def test_claim_refuses_changed_current_configuration(store: DraftStore) -> None:
    draft = _draft(store)
    with pytest.raises(ToolError, match="configured"):
        store.claim(draft["publish_args"], author_urn="urn:li:person:OTHER", api_version="202610")
    with pytest.raises(ToolError, match="configured"):
        store.claim(draft["publish_args"], author_urn=AUTHOR, api_version="202611")


def test_only_one_concurrent_claim_can_succeed(store: DraftStore) -> None:
    draft = _draft(store)

    def claim(_index: int) -> bool:
        try:
            store.claim(draft["publish_args"], author_urn=AUTHOR, api_version="202610")
        except ToolError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(results) == [False, True]
    assert store.read(draft["draft_id"])["state"] == "IN_FLIGHT"


def test_only_one_draft_can_be_in_flight_across_the_connector(store: DraftStore) -> None:
    first, second = _draft(store), _draft(store, "Second body.")
    store.claim(first["publish_args"], author_urn=AUTHOR, api_version="202610")
    with pytest.raises(ToolError, match="another"):
        store.claim(second["publish_args"], author_urn=AUTHOR, api_version="202610")
    store.finish(first["draft_id"], "UNKNOWN", reason="transport_error")
    store.claim(second["publish_args"], author_urn=AUTHOR, api_version="202610")


@pytest.mark.parametrize("state", ["PUBLISHED", "REFUSED", "UNKNOWN"])
def test_every_completed_attempt_is_permanently_consumed(store: DraftStore, state: PublishState) -> None:
    draft = _draft(store)
    store.claim(draft["publish_args"], author_urn=AUTHOR, api_version="202610")
    store.finish(draft["draft_id"], state, post_urn="urn:li:share:123" if state == "PUBLISHED" else None)
    with pytest.raises(ToolError, match="no retry"):
        store.claim(draft["publish_args"], author_urn=AUTHOR, api_version="202610")
    assert store.read(draft["draft_id"])["state"] == state
    events = store.ctx.events.recent(source="linkedin")
    assert sum(event.type == "linkedin.post_published" for event in events) == int(state == "PUBLISHED")


def test_list_is_bounded_and_full_text_is_available_only_by_identifier(store: DraftStore) -> None:
    draft = _draft(store, "x" * 3000)
    _draft(store, "Another draft.")
    listing = store.list(1)
    assert listing["truncated"] is True
    assert len(listing["drafts"]) == 1
    assert "publish_args" not in listing["drafts"][0]
    found = store.read(draft["draft_id"])
    assert found["text"] == "x" * 3000
    assert found["publish_args"]["text"] == found["text"]


def test_draft_event_has_metadata_but_no_body(store: DraftStore) -> None:
    draft = _draft(store)
    [event] = store.ctx.events.recent(source="linkedin")
    assert event.type == "linkedin.draft_created"
    assert event.payload == {
        "draft_id": draft["draft_id"],
        "draft_hash": draft["draft_hash"],
        "author_urn": AUTHOR,
        "visibility": "PUBLIC",
        "characters": len(draft["text"]),
    }


@pytest.mark.parametrize("state", ["PUBLISHED", "REFUSED", "UNKNOWN"])
def test_recording_a_result_without_an_in_flight_claim_is_an_uncertain_outcome(
    store: DraftStore, state: PublishState
) -> None:
    draft = _draft(store)
    with pytest.raises(UncertainOutcomeError, match="reconcile delivery manually"):
        store.finish(draft["draft_id"], state)
    assert store.read(draft["draft_id"])["state"] == "DRAFT"
