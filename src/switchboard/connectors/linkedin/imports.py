"""Declared LinkedIn connection and message snapshots; no links or attachments are fetched."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from switchboard.connectors._archive import (
    MAX_IMPORT_BYTES,
    MAX_RECORDS,
    MAX_TEXT_CHARS,
    ArchiveRecord,
    ImportFormatError,
    decode_text,
    zip_members,
)
from switchboard.util import canonical_json, sha256_hex

PARSER_VERSION = "linkedin-csv-v1"
MAX_PREAMBLE_ROWS = 16
_FILENAMES = {"connections.csv": "connections", "messages.csv": "messages"}
_HEADERS = {
    "connections": {
        "first name": "first_name",
        "last name": "last_name",
        "url": "profile_url",
        "profile url": "profile_url",
        "email address": "email_address",
        "company": "company",
        "position": "position",
        "connected on": "connected_on",
    },
    "messages": {
        "conversation id": "conversation_id",
        "conversation title": "conversation_title",
        "from": "sender",
        "sender profile url": "sender_profile_url",
        "to": "recipients",
        "recipient profile urls": "recipient_profile_urls",
        "recipient profile url": "recipient_profile_urls",
        "date": "date",
        "subject": "subject",
        "content": "content",
        "folder": "folder",
    },
}
_REQUIRED = {
    "connections": {"first_name", "last_name", "connected_on"},
    "messages": {"conversation_id", "sender", "recipients", "date", "content"},
}


@dataclass(frozen=True)
class ParsedExport:
    records: tuple[ArchiveRecord, ...]
    datasets: tuple[str, ...]
    file_key: str


def parse_export(raw: bytes, filename: str, format_hint: str | None = None) -> ParsedExport:
    """Parse only explicitly supported datasets, with a checksum and parser-context snapshot identity."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportFormatError("the export exceeds the import size limit")
    suffix = Path(filename).suffix.lower()
    sources: list[tuple[str, bytes]] = []
    if suffix == ".zip":
        if format_hint is not None:
            raise ImportFormatError("format is only for direct CSV files, not archives")
        for member in zip_members(raw, (".csv",)):
            dataset = _FILENAMES.get(PurePosixPath(member.name).name.casefold())
            if dataset is not None:
                sources.append((dataset, member.data))
        if len({dataset for dataset, _body in sources}) != len(sources):
            raise ImportFormatError("an archive may contain only one export of each supported dataset")
    elif suffix == ".csv":
        dataset = format_hint if format_hint is not None else _FILENAMES.get(filename.casefold())
        if dataset not in _HEADERS:
            raise ImportFormatError("use Connections.csv or messages.csv, or supply an explicit CSV format")
        sources.append((dataset, raw))
    else:
        raise ImportFormatError("only declared CSV files or ZIP archives are supported")
    if not sources:
        raise ImportFormatError("the archive contains no Connections.csv or messages.csv")
    records: list[ArchiveRecord] = []
    for dataset, body in sources:
        for record in _records(body, dataset):
            records.append(record)
            if len(records) > MAX_RECORDS:
                raise ImportFormatError(f"exports may contain at most {MAX_RECORDS} records in total")
    if not records:
        raise ImportFormatError("the export contains no supported records")
    datasets = tuple(sorted(dataset for dataset, _body in sources))
    file_key = sha256_hex(
        canonical_json({"checksum": sha256_hex(raw), "parser": PARSER_VERSION, "datasets": datasets})
    )
    return ParsedExport(tuple(records), datasets, file_key)


def _header(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _columns(row: list[str], dataset: str) -> list[str]:
    aliases = _HEADERS[dataset]
    names = [_header(value) for value in row]
    if not names or any(name not in aliases for name in names):
        raise ImportFormatError(f"unsupported {dataset} CSV headers")
    columns = [aliases[name] for name in names]
    if len(set(columns)) != len(columns):
        raise ImportFormatError(f"duplicate {dataset} CSV headers")
    if not _REQUIRED[dataset].issubset(columns):
        raise ImportFormatError(f"missing required {dataset} CSV headers")
    return columns


def _records(raw: bytes, dataset: str) -> Iterator[ArchiveRecord]:
    reader = csv.reader(io.StringIO(decode_text(raw), newline=""), strict=True)
    columns: list[str] | None = None
    preamble = False
    try:
        for row_number, row in enumerate(reader, 1):
            if row_number > MAX_RECORDS + MAX_PREAMBLE_ROWS + 1:
                raise ImportFormatError("the CSV exceeds the logical row limit")
            if any(len(value) > MAX_TEXT_CHARS or "\x00" in value for value in row):
                raise ImportFormatError(
                    f"CSV fields must contain at most {MAX_TEXT_CHARS} non-null characters"
                )
            if not row or all(not value.strip() for value in row):
                continue
            if columns is None:
                if row_number > MAX_PREAMBLE_ROWS:
                    raise ImportFormatError("the CSV header must appear within the first 16 logical rows")
                if dataset == "connections":
                    if len(row) == 1 and _header(row[0]) == "notes:" and not preamble:
                        preamble = True
                        continue
                    if preamble and (
                        len(row) == 1
                        or row[0].strip().casefold().startswith("when exporting your connection data")
                    ):
                        continue
                columns = _columns(row, dataset)
                continue
            if len(row) != len(columns):
                raise ImportFormatError(f"a {dataset} CSV row has the wrong number of fields")
            data = dict.fromkeys(_HEADERS[dataset].values(), "")
            data.update(zip(columns, row, strict=True))
            if dataset == "connections":
                if not data["connected_on"].strip() or not (data["first_name"] + data["last_name"]).strip():
                    raise ImportFormatError("a connection needs a name and a connection date")
                yield ArchiveRecord("connection", "Connections", data)
            else:
                if not data["conversation_id"].strip() or not data["date"].strip():
                    raise ImportFormatError("a message needs a conversation identifier and a date")
                if len(data["conversation_id"]) > 256:
                    raise ImportFormatError("conversation identifiers must contain at most 256 characters")
                yield ArchiveRecord("message", data["conversation_id"], data)
    except csv.Error:
        raise ImportFormatError("the CSV is malformed or contains an oversized field") from None
    if columns is None:
        raise ImportFormatError(f"missing required {dataset} CSV headers")
