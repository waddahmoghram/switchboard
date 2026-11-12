"""Local archive tools and one narrowly scoped, explicitly approved external action."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from switchboard.connectors._archive import ArchiveStore, read_import_file
from switchboard.connectors.linkedin.drafts import DraftStore
from switchboard.connectors.linkedin.imports import parse_export
from switchboard.connectors.linkedin.settings import (
    AUTHOR_PATTERN,
    MAX_POST_CHARS,
    NAME,
    VERSION_PATTERN,
    VISIBILITIES,
    Settings,
    load_settings,
)
from switchboard.context import AppContext
from switchboard.errors import ConfigError, SecretError, ToolError, UncertainOutcomeError
from switchboard.policy import Tier
from switchboard.tools import Tool
from switchboard.util import canonical_json, truncate

POSTS_URL = "https://api.linkedin.com/rest/posts"
_POST_URN = re.compile(r"urn:li:(?:share|ugcPost):[0-9]{1,64}")
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_DRAFT_ID = {"type": "string", "minLength": 32, "maxLength": 32, "pattern": "^[0-9a-f]{32}$"}
_TEXT = {"type": "string", "minLength": 1, "maxLength": MAX_POST_CHARS}
_VISIBILITY = {"type": "string", "enum": list(VISIBILITIES), "maxLength": 16}
_PUBLISH_PROPERTIES = {
    "draft_id": _DRAFT_ID,
    "draft_hash": {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"},
    "text": _TEXT,
    "visibility": _VISIBILITY,
    "author_urn": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": f"^{AUTHOR_PATTERN}$"},
    "api_version": {"type": "string", "minLength": 6, "maxLength": 6, "pattern": f"^{VERSION_PATTERN}$"},
}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _check_text(text: str) -> None:
    if (
        not text.strip()
        or len(text) > MAX_POST_CHARS
        or any((ord(char) < 32 or 127 <= ord(char) <= 159) and char not in "\n\t" for char in text)
    ):
        raise ToolError("post text must be non-blank, at most 3000 characters, with no control characters")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ToolError("post text must contain valid Unicode characters") from None


def _summary(args: Mapping[str, Any]) -> str:
    return (
        f"Publish LinkedIn draft {args['draft_id']} hash {args['draft_hash']} as {args['author_urn']} "
        f"({args['visibility']}, {len(args['text'])} characters). "
        f"Preview: {truncate(args['text'], 80)}. Review the full stored arguments."
    )


class LinkedInService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.archive = ArchiveStore(ctx, NAME)
        self.store = DraftStore(ctx)

    def settings(self) -> Settings:
        try:
            return load_settings(self.ctx.connector_config(NAME))
        except ConfigError:
            raise ToolError("LinkedIn configuration is invalid; check the connector settings") from None

    def import_export(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = read_import_file(self.settings().import_dir, args["filename"], (".csv", ".zip"))
        parsed = parse_export(raw, args["filename"], args.get("format"))
        return self.archive.import_records(
            parsed.file_key,
            args["filename"],
            parsed.records,
            details={"datasets": list(parsed.datasets)},
        )

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.archive.search(
            args.get("query", ""),
            kind=args.get("kind"),
            label=args.get("conversation_id"),
            limit=args.get("limit", 20),
        )

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.archive.read(args["id"])

    def draft_post(self, args: dict[str, Any]) -> dict[str, Any]:
        _check_text(args["text"])
        settings = self.settings()
        return self.store.create(
            args["text"], args.get("visibility", "PUBLIC"), settings.author_urn, settings.api_version
        )

    def drafts(self, args: dict[str, Any]) -> dict[str, Any]:
        if "draft_id" in args:
            return {"drafts": [self.store.read(args["draft_id"])], "truncated": False}
        return self.store.list(args.get("limit", 20))

    def publish_post(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        if not settings.publishing_enabled:
            raise ToolError("LinkedIn publishing is disabled; no request was sent")
        _check_text(args["text"])
        try:
            token = self.ctx.secret(settings.access_token)
        except SecretError:
            raise ToolError("the LinkedIn access token could not be resolved; no request was sent") from None
        if len(token) > 16384 or _BEARER.fullmatch(token) is None:
            raise ToolError("the LinkedIn access token is not a valid bearer credential; no request was sent")
        if settings != self.settings():
            raise ToolError("LinkedIn configuration changed before publication; no request was sent")
        approved = self.store.claim(args, author_urn=settings.author_urn, api_version=settings.api_version)
        body = canonical_json(
            {
                "author": approved["author_urn"],
                "commentary": approved["text"],
                "visibility": approved["visibility"],
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            }
        ).encode("ascii")
        try:
            response = self.ctx.http.request(
                "POST",
                POSTS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "LinkedIn-Version": approved["api_version"],
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=body,
                timeout=20.0,
                max_bytes=64000,
                follow_redirects=False,
            )
        except Exception:
            # Transport exceptions can include credentials, and cannot prove whether delivery occurred.
            self.store.finish(args["draft_id"], "UNKNOWN", reason="transport_error")
            raise UncertainOutcomeError(
                "LinkedIn delivery is UNKNOWN after a transport failure; "
                "inspect your profile; no retry is allowed"
            ) from None
        post_ids = [value for key, value in response.headers.items() if key.casefold() == "x-restli-id"]
        if (
            response.status == 201
            and response.url == POSTS_URL
            and len(post_ids) == 1
            and _POST_URN.fullmatch(post_ids[0]) is not None
        ):
            post_urn = post_ids[0]
            self.store.finish(args["draft_id"], "PUBLISHED", http_status=201, post_urn=post_urn)
            return {"state": "PUBLISHED", "draft_id": args["draft_id"], "post_urn": post_urn}
        if 400 <= response.status < 500 and response.status != 408:
            self.store.finish(
                args["draft_id"], "REFUSED", http_status=response.status, reason="provider_refusal"
            )
            raise ToolError(
                f"LinkedIn refused publication (status {response.status}); this draft cannot be retried"
            )
        self.store.finish(
            args["draft_id"], "UNKNOWN", http_status=response.status, reason="unconfirmed_delivery"
        )
        raise UncertainOutcomeError(
            "LinkedIn delivery is UNKNOWN without reliable confirmation; "
            "inspect your profile; no retry is allowed"
        )


def build_tools(service: LinkedInService) -> list[Tool]:
    return [
        Tool(
            "linkedin_import_export",
            "Import your editable historical Connections.csv or messages.csv, directly or in a ZIP. "
            "No live inbox.",
            Tier.DRAFT,
            _schema(
                {
                    "filename": {"type": "string", "minLength": 1, "maxLength": 255},
                    "format": {"type": "string", "enum": ["connections", "messages"], "maxLength": 16},
                },
                ("filename",),
            ),
            service.import_export,
            connector=NAME,
        ),
        Tool(
            "linkedin_search",
            "Search local export snapshots only. "
            "Imported names and message senders are not verified identities.",
            Tier.READ,
            _schema(
                {
                    "query": {"type": "string", "maxLength": 500},
                    "kind": {"type": "string", "enum": ["connection", "message"], "maxLength": 16},
                    "conversation_id": {"type": "string", "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                }
            ),
            service.search,
            connector=NAME,
        ),
        Tool(
            "linkedin_read",
            "Read one untrusted historical export record by local identifier; never follows exported links.",
            Tier.READ,
            _schema({"id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}}, ("id",)),
            service.read,
            connector=NAME,
        ),
        Tool(
            "linkedin_draft_post",
            "Save an immutable local text post. Changing any content or author requires a new draft. "
            "Does not publish.",
            Tier.DRAFT,
            _schema({"text": _TEXT, "visibility": _VISIBILITY}, ("text",)),
            service.draft_post,
            connector=NAME,
        ),
        Tool(
            "linkedin_drafts",
            "List local draft previews and delivery states, "
            "or pass draft_id for its full text and exact publish_args.",
            Tier.READ,
            _schema(
                {
                    "draft_id": _DRAFT_ID,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                }
            ),
            service.drafts,
            connector=NAME,
        ),
        Tool(
            "linkedin_publish_post",
            "Publish an exact immutable draft to your configured personal profile, once, "
            "only when enabled and approved.",
            Tier.ACT,
            _schema(_PUBLISH_PROPERTIES, tuple(_PUBLISH_PROPERTIES)),
            service.publish_post,
            summarize=_summary,
            recipients=lambda args: [args["author_urn"]],
            connector=NAME,
        ),
    ]
