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
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from src.core.exceptions import PayloadTooLargeError, ValidationError

# Proposed cap: 5 MiB. A "1 row = 1 dossier" contact export is a few hundred
# KB even for thousands of rows; 5 MiB leaves wide margin while refusing a
# pathological upload before it is parsed into memory.
MAX_CSV_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    rows: list[dict[str, str]]
    row_indices: list[int] = field(default_factory=list, compare=False)
    source_rows: dict[int, int] = field(default_factory=dict, compare=False)

    def numbered_rows(self) -> Iterator[tuple[int, dict[str, str]]]:
        """Keep correction references stable when blank records are skipped."""
        return iter(zip(self.row_indices or range(1, len(self.rows) + 1), self.rows, strict=True))


def is_contact_header(value: str) -> bool:
    """Exclude unnamed columns and explicitly labelled worksheet legends."""
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return bool(normalized) and re.match(r"^(?:legende|legend)\s*:", normalized) is None


def _decode(content: bytes | str) -> str:
    """Decode to text, stripping a UTF-8 BOM. `utf-8-sig` removes a leading
    BOM if present and is a no-op otherwise; a str input may already carry a
    BOM char, stripped explicitly."""
    if isinstance(content, str):
        return content.lstrip("﻿")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("File is not valid UTF-8 text.", code="import.file_not_utf8") from exc


def _detect_delimiter(first_line: str) -> str:
    """Comma vs semicolon, by majority count on the header line. Defaults to
    comma on a tie or a single column (no delimiter present)."""
    return ";" if first_line.count(";") > first_line.count(",") else ","


def parse_csv(
    content: bytes | str, *, max_bytes: int = MAX_CSV_BYTES, keep_empty_rows: bool = False
) -> ParsedCsv:
    size = len(content if isinstance(content, bytes) else content.encode("utf-8"))
    if size > max_bytes:
        raise PayloadTooLargeError(
            f"CSV exceeds the {max_bytes}-byte limit.",
            code="import.file_too_large",
            params={"max_bytes": max_bytes, "format": "csv"},
        )

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
    row_indices: list[int] = []
    for row_index, record in enumerate(records[1:], start=1):
        if not keep_empty_rows and all(cell.strip() == "" for cell in record):
            continue  # blank line (trailing newline, separator-only row)
        # Map by position; missing trailing cells → "", extra cells dropped.
        rows.append(
            {
                header: (record[index].strip() if index < len(record) else "")
                for index, header in enumerate(headers)
            }
        )
        row_indices.append(row_index)
    return ParsedCsv(headers=headers, rows=rows, row_indices=row_indices)


# --- XLSX (Excel) ---------------------------------------------------------------
#
# Some CRMs (TeamLeader…) export .xlsx, not CSV. parse_xlsx yields the SAME
# {headers, rows} shape as parse_csv (str values, trimmed, blank rows skipped)
# so validation / dry-run / engine / mapping are untouched. Only the FIRST /
# active sheet is read (documented); other sheets are ignored.

# Same 5 MiB ceiling as the CSV path (consistent across upload formats).
MAX_XLSX_BYTES = MAX_CSV_BYTES
_XLSX_MAGIC = b"PK\x03\x04"  # an .xlsx is a ZIP container


def _xlsx_cell_to_str(value: object) -> str:
    """Normalize an openpyxl-typed cell to the SAME kind of string a CSV would
    carry. The date trap (#1): Excel date/datetime cells come back as
    date/datetime objects → ISO YYYY-MM-DD (never the Excel serial, never a
    datetime with a time component), so the date validation behaves as for CSV.
    """
    if value is None:
        return ""
    if isinstance(value, bool):  # bool BEFORE int (bool is an int subclass)
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):  # datetime BEFORE date (datetime ⊂ date)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Excel stores integers as floats — render 42.0 as "42", keep 75.5.
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def parse_xlsx(
    content: bytes, *, max_bytes: int = MAX_XLSX_BYTES, keep_empty_rows: bool = False
) -> ParsedCsv:
    if len(content) > max_bytes:
        raise PayloadTooLargeError(
            f"XLSX exceeds the {max_bytes}-byte limit.",
            code="import.file_too_large",
            params={"max_bytes": max_bytes, "format": "xlsx"},
        )
    try:
        # read_only: stream rows without loading the whole sheet into memory;
        # data_only: cached values, not formulas.
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ValidationError(
            "File is not a valid .xlsx workbook.", code="import.xlsx_invalid"
        ) from exc
    try:
        sheet = workbook.active  # first / active sheet only
        if sheet is None:
            raise ValidationError(
                "The .xlsx file has no readable sheet.", code="import.xlsx_no_sheet"
            )
        records = [
            [_xlsx_cell_to_str(cell) for cell in row] for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()

    # First NON-EMPTY row = headers (same rule as parse_csv).
    header_index = next(
        (i for i, rec in enumerate(records) if any(cell.strip() != "" for cell in rec)),
        None,
    )
    if header_index is None:
        raise ValidationError(
            "The .xlsx file is empty or has no header row.", code="import.xlsx_empty"
        )
    header_cells = [cell.strip() for cell in records[header_index]]
    columns = [i for i, label in enumerate(header_cells) if is_contact_header(label)]
    # A structured table is not a data boundary: adjacent named columns
    # may contain contact values even when the Excel table stops earlier.
    headers = [header_cells[i] for i in columns]
    legend_columns = {
        i for i, label in enumerate(header_cells) if label and not is_contact_header(label)
    }
    rows: list[dict[str, str]] = []
    row_indices: list[int] = []
    source_rows: dict[int, int] = {}
    for row_index, record in enumerate(records[header_index + 1 :], start=1):
        if not keep_empty_rows and all(
            cell.strip() == "" for i, cell in enumerate(record) if i not in legend_columns
        ):
            continue  # blank row
        rows.append(
            {
                header_cells[index]: (record[index].strip() if index < len(record) else "")
                for index in columns
            }
        )
        row_indices.append(row_index)
        source_rows[row_index] = header_index + row_index + 1
    return ParsedCsv(headers=headers, rows=rows, row_indices=row_indices, source_rows=source_rows)


# --- unified entry point --------------------------------------------------------


def _looks_xlsx(filename: str | None, content: bytes | str) -> bool:
    """Route by filename extension (the content-type proxy we have in JSON),
    with a ZIP-magic sniff when the extension is absent/ambiguous."""
    if filename:
        lowered = filename.strip().lower()
        if lowered.endswith(".xlsx"):
            return True
        if lowered.endswith(".csv"):
            return False
    return isinstance(content, bytes | bytearray) and bytes(content[:4]) == _XLSX_MAGIC


def parse_upload(
    filename: str | None,
    content: bytes | str,
    *,
    max_bytes: int = MAX_CSV_BYTES,
    keep_empty_rows: bool = False,
) -> ParsedCsv:
    """THE single reader the import chain calls. Routes to parse_xlsx or
    parse_csv; both return the same ParsedCsv, so nothing downstream changes."""
    if _looks_xlsx(filename, content):
        if not isinstance(content, bytes | bytearray):
            raise ValidationError(
                "An .xlsx upload must be provided as file bytes.", code="import.xlsx_requires_file"
            )
        return parse_xlsx(bytes(content), max_bytes=max_bytes, keep_empty_rows=keep_empty_rows)
    return parse_csv(content, max_bytes=max_bytes, keep_empty_rows=keep_empty_rows)
