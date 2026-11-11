"""Hash-chained audit log: chaining, tamper detection, redaction, and file safety."""

from __future__ import annotations

import json
from pathlib import Path

from switchboard.audit import GENESIS, AuditLog, redact


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


def test_empty_log_and_tail(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.head() == GENESIS
    assert log.verify().ok
    for index in range(5):
        log.append("event", index=index)
    assert [record["index"] for record in log.tail(2)] == [3, 4]


def test_log_file_is_private(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("event")
    assert log.path.stat().st_mode & 0o777 == 0o600
