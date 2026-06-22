"""CSV reader for the import socle (BLOC 1) — pure, read-only parsing.

Takes a CSV (bytes or text) → {headers, rows}. Robust to the real-world
exports an agency drops on us:
- UTF-8 with or without a BOM (Excel loves the BOM);
- comma OR semicolon delimiter (FR locale Excel exports ';'), detected;
- surrounding whitespace trimmed on headers and cells;
- fully-blank lines skipped (trailing newline, separator-only rows).

No persistence, no DB, no field mapping — that is BLOC 2/3. Errors are
TYPED Nidria exceptions (handled → clean 4xx), never a raw crash: an
oversized file is 413, non-UTF-8 is 422.
"""

import csv
import io
from dataclasses import dataclass

from src.core.exceptions import PayloadTooLargeError, ValidationError

# Proposed cap: 5 MiB. A "1 row = 1 dossier" contact export is a few hundred
# KB even for thousands of rows; 5 MiB leaves wide margin while refusing a
# pathological upload before it is parsed into memory.
MAX_CSV_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    rows: list[dict[str, str]]


def _decode(content: bytes | str) -> str:
    """Decode to text, stripping a UTF-8 BOM. `utf-8-sig` removes a leading
    BOM if present and is a no-op otherwise; a str input may already carry a
    BOM char, stripped explicitly."""
    if isinstance(content, str):
        return content.lstrip("﻿")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("File is not valid UTF-8 text.") from exc


def _detect_delimiter(first_line: str) -> str:
    """Comma vs semicolon, by majority count on the header line. Defaults to
    comma on a tie or a single column (no delimiter present)."""
    return ";" if first_line.count(";") > first_line.count(",") else ","


def parse_csv(content: bytes | str, *, max_bytes: int = MAX_CSV_BYTES) -> ParsedCsv:
    size = len(content if isinstance(content, bytes) else content.encode("utf-8"))
    if size > max_bytes:
        raise PayloadTooLargeError(f"CSV exceeds the {max_bytes}-byte limit.")

    text = _decode(content)
    if text.strip() == "":
        return ParsedCsv(headers=[], rows=[])

    first_line = next((line for line in text.splitlines() if line.strip() != ""), "")
    delimiter = _detect_delimiter(first_line)

    records = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not records:
        return ParsedCsv(headers=[], rows=[])

    headers = [cell.strip() for cell in records[0]]
    rows: list[dict[str, str]] = []
    for record in records[1:]:
        if all(cell.strip() == "" for cell in record):
            continue  # blank line (trailing newline, separator-only row)
        # Map by position; missing trailing cells → "", extra cells dropped.
        rows.append(
            {
                header: (record[index].strip() if index < len(record) else "")
                for index, header in enumerate(headers)
            }
        )
    return ParsedCsv(headers=headers, rows=rows)
