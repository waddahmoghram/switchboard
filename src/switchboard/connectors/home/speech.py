"""Explicit, privacy-aware speaker announcements through an existing speech engine."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from switchboard.connectors.home import schemas
from switchboard.connectors.home.client import HomeAssistant
from switchboard.connectors.home.controls import HomeControls
from switchboard.connectors.home.settings import HomeSettings
from switchboard.connectors.home.state import (
    number,
    observation,
    require_available,
    require_feature,
    require_leaf,
)
from switchboard.context import AppContext
from switchboard.errors import ConfigError, ToolError
from switchboard.util import now, sha256_hex


def require_speech_policy(ctx: AppContext, settings: HomeSettings) -> None:
    if not settings.speech.enabled:
        return
    rule = ctx.config.section("policy", "tools").get("home_announce")
    if not isinstance(rule, Mapping) or rule.get("decision") not in {"ask", "deny"}:
        raise ConfigError(
            "enabled home speech requires an exact [policy.tools.home_announce] decision = 'ask' or 'deny'; "
            "unattended announcements are not supported"
        )


class HomeSpeech:
    def __init__(self, client: HomeAssistant, settings: HomeSettings, controls: HomeControls) -> None:
        self.client = client
        self.settings = settings
        self.controls = controls

    def summarize(self, args: Mapping[str, Any]) -> str:
        text = json.dumps(args["text"], ensure_ascii=True)
        external = "yes" if self.settings.speech.external_processing else "no"
        return (
            f"Speak {text} via {args['engine']} to {args['entity_id']} at {args['volume_level']}; "
            f"sensitive={args['sensitive']}; external={external}"
        )

    def announce(self, args: dict[str, Any]) -> dict[str, Any]:
        schemas.checked_action(args, schemas.ANNOUNCE, self.settings.binding)
        speech = self.settings.speech
        if not speech.enabled:
            raise ToolError("home speech is disabled")
        if args["engine"] != speech.engine or args["entity_id"] not in speech.destinations:
            raise ToolError("the exact engine and destination must match the configured speech allowlist")
        text = args["text"]
        if not text.strip() or "<" in text or ">" in text:
            raise ToolError("announcements need nonempty plain text, not markup")
        if args["sensitive"] and not speech.allow_sensitive:
            raise ToolError("sensitive speaker announcements are disabled")
        if speech.external_processing and (not speech.allow_external or args["sensitive"]):
            raise ToolError("the configured speech privacy restrictions refuse this announcement")
        if self.settings.quiet_hours.active(now()):
            raise ToolError("quiet hours block speaker announcements")
        entity_id = args["entity_id"]
        self.controls.target(entity_id, "media_player")
        limits = self.settings.media_players[entity_id]
        if not limits.minimum <= args["volume_level"] <= limits.maximum:
            raise ToolError("the approved announcement volume exceeds configured safe bounds")
        engine = self.client.state(speech.engine)
        require_leaf(engine, (speech.engine,))
        raw = self.controls.read_target(entity_id)
        attributes = require_available(raw)
        require_feature(attributes, 512, "speech media playback")
        volume = self.controls.safe_volume(entity_id, attributes)
        if abs(volume - args["volume_level"]) > 0.01:
            raise ToolError(
                "the observed volume differs from the approved volume; adjust it separately first"
            )
        if attributes.get("is_volume_muted") is not False:
            raise ToolError("the speaker is muted or its mute state is unknown; no announcement was sent")
        if self.settings.quiet_hours.active(now()):
            raise ToolError("quiet hours started during preflight; no announcement was sent")
        receipt = self.client.submit(
            "tts",
            "speak",
            {
                "entity_id": speech.engine,
                "media_player_entity_id": entity_id,
                "message": text,
                "cache": False,
            },
        )
        observed: dict[str, Any] | None = None
        readback_error: str | None = None
        warning: str | None = None
        if receipt.accepted is not False:
            try:
                observed = observation(
                    entity_id, self.client.state(entity_id), now().isoformat(timespec="microseconds")
                )
                after = number(observed["attributes"].get("volume_level"))
                if (
                    after is None
                    or not limits.minimum <= after <= limits.maximum
                    or abs(after - volume) > 0.01
                ):
                    warning = (
                        "volume changed or became unknown after submission; no compensating action was sent"
                    )
            except ToolError:
                readback_error = "speaker readback failed; the announcement was not resent"
        return {
            "engine": speech.engine,
            "entity_id": entity_id,
            "text_hash": sha256_hex(text),
            "characters": len(text),
            "volume_level": args["volume_level"],
            "sensitive": args["sensitive"],
            "external_processing": speech.external_processing,
            "request_status": "accepted"
            if receipt.accepted is True
            else ("rejected" if receipt.accepted is False else "unknown"),
            "status": "rejected" if receipt.accepted is False else "requested_unconfirmed",
            "confirmed": False,
            "delivery_verified": False,
            "limitation": "acceptance or a playing state cannot verify that this text was heard",
            "observed_player": observed,
            "error": receipt.error,
            "readback_error": readback_error,
            "warning": warning,
            "retry_safe": False,
        }
