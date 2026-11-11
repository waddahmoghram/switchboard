"""Offline configuration validation, including a deliberate disabled default."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard.connectors.linkedin import doctor, register
from switchboard.connectors.linkedin.settings import load_settings
from switchboard.errors import ConfigError
from tests.conftest import AppFactory
from tests.fakes import FakeHttp


def test_publication_is_disabled_and_credentials_are_optional_by_default() -> None:
    settings = load_settings({})
    assert settings.publishing_enabled is False
    assert settings.author_urn == ""
    assert settings.access_token == ""
    assert settings.import_dir is None
    assert settings.api_version == "202610"


@pytest.mark.parametrize(
    "settings",
    [
        {"publishing_enabled": "false"},
        {"publishing_enabled": True},
        {"publishing_enabled": True, "author_urn": "urn:li:person:SYNTHETIC"},
        {"access_token": "a-raw-token-is-not-a-reference"},
        {"author_urn": "https://www.linkedin.com/in/synthetic-owner"},
        {"author_urn": "urn:li:organization:123"},
        {"author_urn": "urn:li:person:SYNTHETIC\n"},
        {"api_version": "latest"},
        {"api_version": "202613"},
        {"api_version": 202610},
        {"import_dir": 123},
        {"import_dir": "folder\x00"},
        {"publishing_enabld": True},
    ],
)
def test_invalid_or_unsafe_settings_are_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load_settings(settings)


def test_registration_and_disabled_doctor_never_resolve_a_secret_or_use_the_network(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(
        {
            "connectors": {
                "linkedin": {
                    "publishing_enabled": False,
                    "author_urn": "urn:li:person:SYNTHETIC",
                    "access_token": "env:SYNTHETIC_LINKEDIN_TOKEN",
                }
            }
        }
    )

    def forbidden(_reference: str) -> str:
        pytest.fail("registration and disabled diagnostics must not read secrets")

    monkeypatch.setattr(app.ctx, "secret", forbidden)
    register(app.ctx, app.registry)
    assert doctor(app.ctx) == []
    assert fake_http.requests == []
