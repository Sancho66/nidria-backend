"""CSV/ZIP writing for the agency data export — stdlib only, zero new dep.

The import side (src/imports) only ever READS (csv.reader); this is the
mirror WRITER. A CSV carries a UTF-8 BOM so Excel opens French accents
correctly; the ZIP bundles the named CSVs (deflated)."""

import csv
import io
import json
import zipfile
from typing import Any


def _cell(value: Any) -> str:
    """One CSV cell — the export's single stringification rule."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, (list, tuple)):
        return "; ".join(_cell(v) for v in value)
    if isinstance(value, dict):
        # A JSONB blob (activity details) — compact, stable, UTF-8 kept.
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def render_csv(header: list[str], rows: list[list[Any]]) -> str:
    """A CSV string (BOM + CRLF, Excel-friendly). Cells are stringified
    by `_cell`; the writer quotes/escapes commas and newlines itself."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(cell) for cell in row])
    return "﻿" + buffer.getvalue()


def build_zip(files: dict[str, str]) -> bytes:
    """A deflated ZIP of named UTF-8 text files, in insertion order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()
