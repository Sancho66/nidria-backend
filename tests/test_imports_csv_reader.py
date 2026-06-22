"""CSV reader (BLOC 1) — BOM, delimiter detection, empties, limits.

Pure unit tests: no DB, no HTTP.
"""

import pytest

from src.core.exceptions import PayloadTooLargeError, ValidationError
from src.imports.csv_reader import MAX_CSV_BYTES, parse_csv


def test_comma_delimiter_basic() -> None:
    parsed = parse_csv("email,first_name\nalice@x.io,Alice\n")
    assert parsed.headers == ["email", "first_name"]
    assert parsed.rows == [{"email": "alice@x.io", "first_name": "Alice"}]


def test_semicolon_delimiter_detected() -> None:
    # FR-locale Excel exports use ';'
    parsed = parse_csv("email;first_name;dest\nbob@x.io;Bob;PY\n")
    assert parsed.headers == ["email", "first_name", "dest"]
    assert parsed.rows == [{"email": "bob@x.io", "first_name": "Bob", "dest": "PY"}]


def test_utf8_bom_is_stripped_from_bytes() -> None:
    raw = "﻿email,name\nz@x.io,Zoé\n".encode()
    parsed = parse_csv(raw)
    # the BOM must NOT cling to the first header
    assert parsed.headers == ["email", "name"]
    assert parsed.rows[0]["name"] == "Zoé"


def test_utf8_bom_is_stripped_from_str() -> None:
    parsed = parse_csv("﻿email,name\nz@x.io,Zoe\n")
    assert parsed.headers == ["email", "name"]


def test_whitespace_trimmed_and_blank_lines_skipped() -> None:
    parsed = parse_csv("  email , name \n  a@x.io ,  Al  \n\n   \n")
    assert parsed.headers == ["email", "name"]
    assert parsed.rows == [{"email": "a@x.io", "name": "Al"}]


def test_empty_cells_preserved_as_empty_string() -> None:
    parsed = parse_csv("email,phone\na@x.io,\n")
    assert parsed.rows == [{"email": "a@x.io", "phone": ""}]


def test_short_row_pads_missing_trailing_cells() -> None:
    parsed = parse_csv("a,b,c\n1,2\n")
    assert parsed.rows == [{"a": "1", "b": "2", "c": ""}]


def test_empty_file_yields_empty_structure() -> None:
    parsed = parse_csv("")
    assert parsed.headers == []
    assert parsed.rows == []


def test_header_only_file() -> None:
    parsed = parse_csv("email,name\n")
    assert parsed.headers == ["email", "name"]
    assert parsed.rows == []


def test_size_limit_raises_413() -> None:
    too_big = b"x" * (MAX_CSV_BYTES + 1)
    with pytest.raises(PayloadTooLargeError):
        parse_csv(too_big)


def test_invalid_utf8_raises_422() -> None:
    with pytest.raises(ValidationError):
        parse_csv(b"\xff\xfe not utf-8 \xff")
