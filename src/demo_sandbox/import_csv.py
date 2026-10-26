"""Parse prospect CSV once and isolate validation failures by source row."""

import csv
import io
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from src.admin.demo_import_schema import MAX_IMPORT_BYTES, MAX_IMPORT_ROWS, DemoImportIssue
from src.admin.demo_schema import AgencyDemoInput
from src.core.exceptions import ValidationError

ALIASES = {
    "companyname": "company_name",
    "company": "company_name",
    "cabinet": "company_name",
    "lead_email": "email",
    "leademail": "email",
    "langue": "language",
    "metier": "profession",
    "pays": "country",
    "ville": "city",
    "logo": "logo_data_url",
}
REQUIRED = (
    "email",
    "company_name",
    "language",
    "profession",
    "country",
    "persona",
    *(f"m2_s{i}" for i in range(1, 7)),
)


@dataclass
class ImportRow:
    number: int
    data: dict[str, Any]
    errors: list[DemoImportIssue]


def field_column(location: tuple[Any, ...]) -> str:
    field = str(location[0]) if location else "CSV"
    if field == "steps":
        return f"m2_s{location[1] + 1}" if len(location) > 1 else "m2_s1–m2_s6"
    if field == "step_titles":
        return "step_title_1–step_title_6"
    return field


def parse_import(text: str) -> list[ImportRow]:
    csv.field_size_limit(MAX_IMPORT_BYTES)
    text = text.removeprefix("\ufeff")
    first = text.splitlines()[0] if text else ""
    separator = ";" if ";" in first else "\t" if "\t" in first else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)
    try:
        header = next(reader, [])
        names = [ALIASES.get(name.strip().lower(), name.strip().lower()) for name in header]
        if len(set(names)) != len(names) or any(key not in names for key in REQUIRED):
            raise ValidationError("Invalid CSV headers.", code="demo.import_headers")
        result: list[ImportRow] = []
        while True:
            number = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if not any(cell.strip() for cell in row):
                continue
            if len(result) == MAX_IMPORT_ROWS:
                raise ValidationError("Import supports 1 to 500 rows.", code="demo.import_size")
            if len(row) != len(names):
                result.append(
                    ImportRow(
                        number,
                        {},
                        [
                            DemoImportIssue(
                                row=number,
                                column="CSV",
                                reason="Column count differs from the header.",
                            )
                        ],
                    )
                )
                continue
            source = dict(zip(names, (cell.strip() for cell in row), strict=True))
            titles = [source.get(f"step_title_{i}", "") for i in range(1, 7)]
            data: dict[str, Any] = {
                key: source.get(key, "")
                for key in (
                    "email",
                    "company_name",
                    "language",
                    "profession",
                    "country",
                    "city",
                    "persona",
                )
            }
            data.update(
                steps=[source[f"m2_s{i}"] for i in range(1, 7)],
                step_titles=titles if any(titles) else [],
                journey_name=source.get("journey_name") or None,
                documents=source["documents"].split("|") if source.get("documents") else [],
                logo_data_url=source.get("logo_data_url") or None,
            )
            try:
                item = AgencyDemoInput.model_validate(data)
                result.append(ImportRow(number, item.model_dump(mode="json"), []))
            except PydanticValidationError as exc:
                issues = [
                    DemoImportIssue(
                        row=number, column=field_column(error["loc"]), reason=error["msg"]
                    )
                    for error in exc.errors(include_input=False, include_context=False)
                ]
                result.append(ImportRow(number, {}, issues))
    except csv.Error as exc:
        raise ValidationError("Invalid CSV quoting.", code="demo.import_csv") from exc
    if not result:
        raise ValidationError("Import supports 1 to 500 rows.", code="demo.import_size")
    return result


def export_csv(fields: list[str], values: list[list[str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(fields)
    for row in values:
        writer.writerow(
            [
                "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                for value in row
            ]
        )
    return "\ufeff" + output.getvalue()
