"""The LinkedIn connector's doctor: settings, the private import folder, and the publishing token.

The doctor reports local problems only: it never calls LinkedIn and never shows the token.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.connectors import linkedin
from switchboard.errors import ConfigError
from tests.conftest import AppFactory
from tests.fakes import FakeHttp

TOKEN_ENV = "SB_TEST_LINKEDIN_TOKEN"
RAW_TOKEN = "AQV-synthetic-raw-token-0000"
PUBLISHING = {
    "publishing_enabled": True,
    "author_urn": "urn:li:person:SYNTHETIC",
    "access_token": f"env:{TOKEN_ENV}",
}
NOT_PRIVATE = "the import folder must be a real directory you own with mode 700"


def _config(**table: Any) -> dict[str, Any]:
    return {"switchboard": {"connectors": ["linkedin"]}, "connectors": {"linkedin": table}}


def _private_folder(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _readable_by_others(tmp_path: Path) -> Path:
    folder = _private_folder(tmp_path / "exports")
    folder.chmod(0o755)
    return folder


def _regular_file(tmp_path: Path) -> Path:
    path = tmp_path / "exports.zip"
    path.write_bytes(b"")
    path.chmod(0o600)
    return path


def _link_to_private_folder(tmp_path: Path) -> Path:
    link = tmp_path / "exports-link"
    link.symlink_to(_private_folder(tmp_path / "exports"), target_is_directory=True)
    return link


def test_a_private_folder_and_a_resolvable_token_have_no_problems_without_a_server_call(
    make_app: AppFactory, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "synthetic-token-0000")
    _private_folder(tmp_path / "exports")
    app = make_app(_config(import_dir="~/exports", **PUBLISHING))
    monkeypatch.setenv("HOME", str(tmp_path))  # the folder is found the way imports read it, after ~

    assert linkedin.doctor(app.ctx) == []
    assert fake_http.requests == []


def test_a_missing_import_folder_is_reported(make_app: AppFactory, tmp_path: Path) -> None:
    app = make_app(_config(import_dir=str(tmp_path / "missing")))
    assert linkedin.doctor(app.ctx) == ["cannot inspect the import folder (No such file or directory)"]


@pytest.mark.parametrize(
    "make_path",
    [_readable_by_others, _regular_file, _link_to_private_folder],
    ids=["mode-755", "regular-file", "symlink-to-a-private-folder"],
)
def test_an_import_folder_that_is_not_a_private_directory_is_reported(
    make_app: AppFactory, tmp_path: Path, make_path: Callable[[Path], Path]
) -> None:
    app = make_app(_config(import_dir=str(make_path(tmp_path))))
    assert linkedin.doctor(app.ctx) == [NOT_PRIVATE]


def test_publishing_without_a_resolvable_token_is_reported(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    app = make_app(_config(**PUBLISHING))
    assert linkedin.doctor(app.ctx) == ["the LinkedIn access token could not be resolved"]


def test_both_problems_are_reported_together(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    app = make_app(_config(import_dir=str(_readable_by_others(tmp_path)), **PUBLISHING))
    assert linkedin.doctor(app.ctx) == [NOT_PRIVATE, "the LinkedIn access token could not be resolved"]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"publishing_enabld": True}, "contains unknown settings"),
        ({"publishing_enabled": True}, "publishing requires your author_urn and an access_token"),
        ({"access_token": RAW_TOKEN}, "access_token must be an env:, file:, or cmd: secret reference"),
        (
            {"author_urn": "https://www.linkedin.com/in/synthetic-owner"},
            "author_urn must be your urn:li:person:",
        ),
    ],
)
def test_doctor_reports_an_invalid_table_as_start_up_would(
    make_app: AppFactory, table: dict[str, Any], message: str
) -> None:
    problems = linkedin.doctor(make_app({"connectors": {"linkedin": table}}).ctx)

    with pytest.raises(ConfigError) as raised:
        make_app(_config(**table))
    assert problems == [str(raised.value)]
    assert message in problems[0]
    assert RAW_TOKEN not in problems[0]  # a token pasted in place of a reference is never repeated
