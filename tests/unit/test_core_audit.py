"""Hash-chained audit log: chaining, tamper detection, redaction, and file safety."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from switchboard import audit
from switchboard.audit import GENESIS, AuditLog, redact
from switchboard.errors import AuditError
from switchboard.policy import Origin, Tier
from switchboard.tools import Tool
from switchboard.util import canonical_json, sha256_hex
from tests.conftest import AppFactory

# Secret-shaped samples are assembled at run time so repository secret scanners do not flag them.
_GITHUB = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_RANDOM = "P82y2v9MqP87-YgS1jKH" + "uyzzeQqYd48NJ7ksfA0yj34"
_DIGEST = "51a9cb1edd4430ebc2b9bf14dc736934" + "81ccba52df9393befae4fb221a7beae3"


def test_chain_links_records_and_verifies(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append("one", value=1)
    second = log.append("two", value=2)
    assert first["prev"] == GENESIS
    assert second["prev"] == first["hash"]
    result = log.verify()
    assert result.ok
    assert result.records == 2
    assert result.head == second["hash"] == log.head()


def test_editing_a_record_is_detected(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("one", value=1)
    log.append("two", value=2)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["value"] = 999
    lines[0] = json.dumps(record)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = log.verify()
    assert not result.ok
    assert "record 1" in (result.problem or "")


def test_deleting_a_record_breaks_the_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(3):
        log.append("event", index=index)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
    result = log.verify()
    assert not result.ok
    assert "record 2" in (result.problem or "")


def test_long_records_are_chained_across_read_buffer_boundaries(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    big = log.append("big", **{f"field{i}": "v" * 200 for i in range(60)})
    assert len(json.dumps(big)) > 8192
    after = log.append("after")
    assert after["prev"] == big["hash"]
    assert log.verify().ok


def test_redaction_hides_credentials_and_long_text() -> None:
    cleaned = redact(
        {
            "password": "not-a-real-password",
            "nested": {"api_key": "k", "note": "x" * 1000},
            "items": list(range(60)),
        }
    )
    assert cleaned["password"] == "[redacted]"
    assert cleaned["nested"]["api_key"] == "[redacted]"
    assert cleaned["nested"]["note"].startswith("x" * 120)
    assert "1000 chars" in cleaned["nested"]["note"]
    assert len(cleaned["items"]) == 50


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer " + "abc.DEF-123_456~789+xyz/==", "Authorization: Bearer [redacted]"),
        (
            'curl -H "Authorization: Basic ' + 'dXNlcjpwYXNzd29yZA==" https://example.com/x',
            'curl -H "Authorization: Basic [redacted]" https://example.com/x',
        ),
        (
            '{"Authorization": "Bearer ' + '0123456789abcdefABCDEF", "Accept": "json"}',
            '{"Authorization": "Bearer [redacted]", "Accept": "json"}',
        ),
        ("Cookie: session=" + "abc123; theme=dark\nnext line", "Cookie: [redacted]\nnext line"),
        ("X-Api-Key: " + "9f8e7d6c5b4a", "X-Api-Key: [redacted]"),
        ("use Bearer " + "A1b2C3d4E5f6G7h8 here", "use Bearer [redacted] here"),
        ("jwt eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0In0.c2lnbmF0dXJl", "jwt [redacted:jwt]"),
        (
            "key:\n-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\nafter",
            "key:\n[redacted:private-key]\nafter",
        ),
        ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA", "[redacted:private-key]"),
        (f"github {_GITHUB}", "github [redacted:github-token]"),
        ("gh" + "o_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "[redacted:github-token]"),
        ("gh" + "s_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "[redacted:github-token]"),
        ("github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop", "[redacted:github-token]"),
        ("xox" + "b-123456789012-1234567890123-AbCdEfGhIjKl", "[redacted:slack-token]"),
        (
            "AKIA" + "IOSFODNN7EXAMPLE and ASIA" + "ABCDEFGHIJKLMNOP",
            "[redacted:aws-key-id] and [redacted:aws-key-id]",
        ),
        ("sk-" + "proj-A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "[redacted:api-key]"),
        ("sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc", "[redacted:api-key]"),
        ("AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q", "[redacted:api-key]"),
        (
            "https://api.telegram.org/bot" + "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaww/getMe",
            "https://api.telegram.org/bot123456789:[redacted]/getMe",
        ),
        ("postgres://alice:" + "s3cr3t@db.example.com/app", "postgres://alice:[redacted]@db.example.com/app"),
        (
            "https://example.com/cb?code=abc&access_token=" + "xyz123&state=ok",
            "https://example.com/cb?code=abc&access_token=[redacted]&state=ok",
        ),
        ("Your password: " + "hunter2 (change it)", "Your password: [redacted] (change it)"),
        ('DB_PASSWORD="' + 'correct horse" other=1', 'DB_PASSWORD="[redacted]" other=1'),
        ("Your OTP: " + "123456, PIN = 0000", "Your OTP: [redacted], PIN = [redacted]"),
        (f"action {_DIGEST[:32]} failed", "action [redacted:hex] failed"),
        (f"sha256 {_DIGEST}", "sha256 [redacted:hex]"),
        (f"random {_RANDOM}", "random [redacted:base64]"),
    ],
)
def test_redaction_scrubs_secret_shapes_inside_text(text: str, expected: str) -> None:
    assert redact({"note": text}) == {"note": expected}
    assert redact(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The bearer of this note, pinned on the spinner mapping",
        "use bearer token-based auth and Bearer abcdefghijklmnop for tests",
        "commit 3f2a9c1 and d71f140abc12 fixed it",
        "id 546bf827-e97d-480c-bff4-21764b46c220",
        "Quarterly-Report-2025-Final-Version-Approved.pdf",
        "ThisIsAVeryLongCamelCaseIdentifierName2 and my_very_long_python_identifier_name_for_testing_v2",
        "IMG_20251126_123456789_HDR_Portrait_Mode.jpg",
        "sk-learn and sk-something-really-long-name-here",
        "Grab a cookie: chocolate chip ones are best",
        "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg\n-----END PUBLIC KEY-----",
        "time 12:34:56 and 2025-11-26T12:00:00+00:00",
        "tokens=5 spin=3 opinion: fine",
        "cafebabe" * 5,
        "1234567890" * 5,
        "averyveryverylongsinglewordwithoutanyseparatorsatall",
    ],
)
def test_redaction_keeps_look_alikes_of_secret_shapes(text: str) -> None:
    assert redact({"note": text}) == {"note": text}


def test_short_sensitive_key_names_match_whole_segments_only() -> None:
    redacted = ["pin", "card_pin", "userPin", "PIN", "otp", "totpSecret", "mfa_code", "2fa", "session"]
    redacted += [
        "session_id",
        "private",
        "private_key",
        "privatekey",
        "PrivateKey",
        "cvv",
        "auth",
        "basic_auth",
    ]
    kept = ["pinned", "spinner", "mapping", "shipping", "opinion", "sessions", "author", "oauth_provider"]
    kept += ["hotpink", "authority", "topic"]
    cleaned = redact({name: "value" for name in redacted + kept})
    assert {name for name, value in cleaned.items() if value == "[redacted]"} == set(redacted)


def test_identifier_keys_keep_digests_but_not_prefixed_tokens() -> None:
    cleaned = redact(
        {
            "action": _DIGEST[:32],
            "manifest_sha256": _DIGEST,
            "target": _DIGEST,
            "message_id": f"<{_RANDOM}@example.com>",
            "draft_id": _GITHUB,
            "note": f"see {_DIGEST}",
        }
    )
    assert cleaned["action"] == _DIGEST[:32]
    assert cleaned["manifest_sha256"] == cleaned["target"] == _DIGEST
    assert cleaned["message_id"] == f"<{_RANDOM}@example.com>"
    assert cleaned["draft_id"] == "[redacted:github-token]"
    assert cleaned["note"] == "see [redacted:hex]"


def test_secrets_are_scrubbed_before_long_text_is_abbreviated() -> None:
    cleaned = redact({"note": "x" * 100 + f" {_GITHUB} " + "y" * 300})
    scrubbed = "x" * 100 + " [redacted:github-token] " + "y" * 300
    assert "gh" + "p_" not in cleaned["note"]
    assert cleaned["note"] == f"{scrubbed[:120]}…[{len(scrubbed)} chars, sha256:{sha256_hex(scrubbed)[:16]}]"


def test_hash_only_fields_keep_length_and_hash_at_any_depth(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", hash_only=("Body",))
    record = log.append(
        "tool.request",
        args={"to": "a@example.com", "BODY": "hello there", "parts": {"body": ["one", {"x": "two"}]}, "n": 3},
        summary="hello there",
    )
    assert record["args"] == {
        "to": "a@example.com",
        "BODY": f"[11 chars, sha256:{sha256_hex('hello there')[:16]}]",
        "parts": {
            "body": [
                f"[3 chars, sha256:{sha256_hex('one')[:16]}]",
                {"x": f"[3 chars, sha256:{sha256_hex('two')[:16]}]"},
            ]
        },
        "n": 3,
    }
    assert record["summary"] == "hello there"
    assert log.verify().ok


def test_configured_hash_only_fields_cover_tool_arguments_and_summaries(make_app: AppFactory) -> None:
    app = make_app({"audit": {"hash_only": ["text", "summary"]}})
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False}
    app.registry.add(
        Tool(
            "demo_say",
            "Say.",
            Tier.ACT,
            schema,
            lambda args: {},
            summarize=lambda args: f"say {args['text']}",
        )
    )
    app.call("demo_say", {"text": "meet at noon"}, Origin.ASSISTANT)
    records = {record["kind"]: record for record in app.ctx.audit.records()}
    assert records["tool.request"]["args"] == {
        "text": f"[12 chars, sha256:{sha256_hex('meet at noon')[:16]}]"
    }
    assert (
        records["approval.created"]["summary"] == f"[16 chars, sha256:{sha256_hex('say meet at noon')[:16]}]"
    )


def test_records_written_under_older_redaction_rules_still_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    with monkeypatch.context() as older:
        older.setattr(audit, "_VALUE_PATTERNS", ())
        older.setattr(audit, "_RANDOM_RUNS", ())
        older.setattr(audit, "_SENSITIVE_SEGMENTS", frozenset())
        log.append("old", pin="1234", note=f"Authorization: Bearer {_RANDOM}")
    log.append("new", pin="1234", note=f"Authorization: Bearer {_RANDOM}")
    old, new = log.records()
    assert (old["pin"], old["note"]) == ("1234", f"Authorization: Bearer {_RANDOM}")
    assert (new["pin"], new["note"]) == ("[redacted]", "Authorization: Bearer [redacted]")
    assert log.verify().ok


def test_empty_log_and_tail(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.head() == GENESIS
    assert log.verify().ok
    for index in range(5):
        log.append("event", index=index)
    assert [record["index"] for record in log.tail(2)] == [3, 4]


def _tail_outcome(log: AuditLog, count: int) -> object:
    try:
        return log.tail(count)
    except AuditError as exc:
        return f"AuditError: {exc}"


def _full_scan_outcome(log: AuditLog, count: int) -> object:
    try:
        return list(log.records())[-count:]
    except AuditError as exc:
        return f"AuditError: {exc}"


def _lines(*records: dict[str, object]) -> bytes:
    return b"".join(canonical_json(record).encode("utf-8") + b"\n" for record in records)


def test_tail_of_a_missing_empty_or_blank_log_is_empty(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.tail(3) == []
    log.path.write_bytes(b"")
    assert log.tail(3) == []
    log.path.write_bytes(b"\n \n\t\r\n")
    assert log.tail(3) == []


def test_tail_returns_every_record_when_the_log_has_fewer(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(3):
        log.append("event", index=index)
    assert [record["index"] for record in log.tail(10)] == [0, 1, 2]


@pytest.mark.parametrize("block", [1, 5, 64, 8192])
def test_tail_matches_a_full_scan_across_block_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: int
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(12):
        log.append("event", index=index, text="x" * (index * 37))
    monkeypatch.setattr(audit, "_BLOCK_SIZE", block)
    everything = list(log.records())
    for count in range(1, 15):
        assert log.tail(count) == everything[-count:]


def test_tail_reads_a_last_record_without_a_trailing_newline(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("one")
    log.append("two")
    log.path.write_bytes(log.path.read_bytes().rstrip(b"\n"))
    assert [record["kind"] for record in log.tail(1)] == ["two"]
    assert [record["kind"] for record in log.tail(5)] == ["one", "two"]


def test_a_partial_last_line_fails_as_a_full_scan_does(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("one")
    log.append("two")
    log.path.write_bytes(log.path.read_bytes() + b'{"kind": "thr')
    with pytest.raises(AuditError, match=r"^record 3: invalid JSON$"):
        log.tail(1)


@pytest.mark.parametrize("ending", [b'{"kind": "thr', b"[1, 2]\n", b"\xff\xfe\n", b"\n\n7\n\n"])
def test_a_damaged_end_reports_what_a_full_scan_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: bytes
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("one")
    log.append("two")
    log.path.write_bytes(log.path.read_bytes() + ending)
    monkeypatch.setattr(audit, "_BLOCK_SIZE", 16)
    for count in (1, 2, 5):
        assert _tail_outcome(log, count) == _full_scan_outcome(log, count)
        assert str(_tail_outcome(log, count)).startswith("AuditError")


def test_tail_splits_lines_and_skips_blank_ones_as_a_full_scan_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    one, two, three, four, five, six = (_lines({"index": index}).rstrip(b"\n") for index in range(6))
    log.path.write_bytes(
        b"".join(
            [one, b"\r\n", two, b"\n\n  \n", three, b"\r", four, b"\r\n\r\n", five, b"\n\x0c\n", six, b"\r"]
        )
    )
    monkeypatch.setattr(audit, "_BLOCK_SIZE", 3)
    everything = list(log.records())
    assert [record["index"] for record in everything] == [0, 1, 2, 3, 4, 5]
    for count in range(1, 8):
        assert log.tail(count) == everything[-count:]


def test_tail_reads_only_as_far_back_as_the_records_it_returns(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.path.write_bytes(b"not json\n" + _lines({"kind": "one"}, {"kind": "two"}))
    assert [record["kind"] for record in log.tail(2)] == ["one", "two"]
    with pytest.raises(AuditError, match=r"^record 1: invalid JSON$"):
        log.tail(3)
    assert not log.verify().ok


def test_log_file_is_private(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("event")
    assert log.path.stat().st_mode & 0o777 == 0o600


def test_head_closes_the_descriptor_of_an_unreadable_log(
    tmp_path: Path, open_descriptors: Callable[[], int]
) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.path.mkdir()
    baseline = open_descriptors()
    with pytest.raises(IsADirectoryError):
        log.head()
    assert open_descriptors() == baseline


@pytest.mark.parametrize("body", [b'{"broken":', b"[]\n", b"\xff\n"])
def test_malformed_logs_report_a_broken_chain_without_a_traceback(tmp_path: Path, body: bytes) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.path.write_bytes(body)
    result = log.verify()
    assert not result.ok
    assert result.problem


def test_tail_deletion_requires_an_independent_head_hash_to_detect(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append("one")
    anchored = log.append("two")["hash"]
    log.path.write_text(log.path.read_text().splitlines()[0] + "\n", encoding="utf-8")
    result = log.verify()
    assert result.ok
    assert result.head == first["hash"]
    assert result.head != anchored
