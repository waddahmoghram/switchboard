"""Hash-chained audit log: chaining, tamper detection, redaction, and file safety."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from switchboard import audit
from switchboard.audit import GENESIS, AuditLog, redact
from switchboard.errors import AuditError
from switchboard.util import canonical_json


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
