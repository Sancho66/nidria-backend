"""XLSX import: parse_xlsx yields the SAME {headers, rows} shape as parse_csv
(so validation / dry-run / engine / mapping are untouched), the unified
parse_upload routes by filename/magic, and an .xlsx import runs end-to-end.

The Excel date trap is covered both at the unit level (datetime → ISO
YYYY-MM-DD) and end-to-end (a "Date de naissance" column surfaces as ISO in
the preview). A corrupt .xlsx is a clean 422, never a crash.
"""

import base64
import io
from datetime import date, datetime

import openpyxl
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import (
    JourneyTemplate,
    JourneyTemplateCaseField,
    JourneyTemplateField,
)
from shared.models.rbac import Role
from src.core.exceptions import PayloadTooLargeError, ValidationError
from src.imports.csv_reader import parse_csv, parse_upload, parse_xlsx
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- parse_xlsx (unit) ----------------------------------------------------------


def test_parse_xlsx_headers_and_rows() -> None:
    parsed = parse_xlsx(_xlsx_bytes([["Prénom", "Nom"], ["Jean", "Martin"], ["  Aïcha  ", "Ba"]]))
    assert parsed.headers == ["Prénom", "Nom"]
    assert parsed.rows == [
        {"Prénom": "Jean", "Nom": "Martin"},
        {"Prénom": "Aïcha", "Nom": "Ba"},  # cells trimmed
    ]


def test_parse_xlsx_excel_date_to_iso() -> None:
    # The trap: a datetime cell (even with a time) → ISO YYYY-MM-DD, no time.
    parsed = parse_xlsx(
        _xlsx_bytes(
            [
                ["Date de naissance"],
                [datetime(1990, 5, 1, 14, 30)],
                [date(1985, 12, 24)],
            ]
        )
    )
    assert parsed.rows == [
        {"Date de naissance": "1990-05-01"},
        {"Date de naissance": "1985-12-24"},
    ]


def test_parse_xlsx_numbers_to_str() -> None:
    parsed = parse_xlsx(_xlsx_bytes([["Code postal", "Taux", "Entier"], [1180, 75.5, 42.0]]))
    assert parsed.rows == [{"Code postal": "1180", "Taux": "75.5", "Entier": "42"}]


def test_parse_xlsx_empty_cell_and_blank_row() -> None:
    parsed = parse_xlsx(
        _xlsx_bytes(
            [
                ["A", "B"],
                ["x", None],  # empty cell → ""
                [None, None],  # blank row → skipped
                ["y", "z"],
            ]
        )
    )
    assert parsed.rows == [{"A": "x", "B": ""}, {"A": "y", "B": "z"}]


def test_parse_xlsx_matches_parse_csv() -> None:
    # Strict output parity: same logical data, CSV vs XLSX → equal ParsedCsv.
    csv = parse_csv("Prénom,Nom\nJean,Martin\n")
    xlsx = parse_xlsx(_xlsx_bytes([["Prénom", "Nom"], ["Jean", "Martin"]]))
    assert csv == xlsx


def test_parse_xlsx_corrupt_raises_validation() -> None:
    with pytest.raises(ValidationError):
        parse_xlsx(b"this is not a real xlsx zip")


def test_parse_xlsx_empty_workbook_raises() -> None:
    with pytest.raises(ValidationError):
        parse_xlsx(_xlsx_bytes([]))


def test_parse_xlsx_size_cap() -> None:
    with pytest.raises(PayloadTooLargeError):
        parse_xlsx(_xlsx_bytes([["A"], ["x"]]), max_bytes=10)


# --- parse_upload (routing) -----------------------------------------------------


def test_parse_upload_routes_by_extension() -> None:
    xlsx = _xlsx_bytes([["A", "B"], ["1", "2"]])
    assert parse_upload("export.xlsx", xlsx).headers == ["A", "B"]
    assert parse_upload("export.csv", "A,B\n1,2\n").headers == ["A", "B"]


def test_parse_upload_sniffs_xlsx_without_extension() -> None:
    # No filename → ZIP magic sniff routes bytes to the xlsx parser.
    xlsx = _xlsx_bytes([["A"], ["1"]])
    assert parse_upload(None, xlsx).headers == ["A"]
    # Plain text bytes with no extension → CSV.
    assert parse_upload(None, "A,B\n1,2\n").headers == ["A", "B"]


# --- end-to-end import from an .xlsx --------------------------------------------


@pytest.fixture
def imports_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def template(
    db_session: AsyncSession,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> JourneyTemplate:
    tmpl = await make_journey_template(agency_id=admin.agency_id)
    await make_template_step(template=tmpl)
    db_session.add(
        JourneyTemplateField(
            template_id=tmpl.id, kind="base_field", reference="date_of_birth", position=0
        )
    )
    db_session.add(
        JourneyTemplateCaseField(template_id=tmpl.id, case_field="dest_country", position=0)
    )
    await db_session.commit()
    return tmpl


def _xlsx_import_body(template: JourneyTemplate) -> dict[str, object]:
    content = _xlsx_bytes(
        [
            ["Email", "First", "Last", "DOB", "Dest"],
            ["zoe@x.io", "Zoé", "Zed", datetime(1990, 5, 1), "PY"],
        ]
    )
    return {
        "journey_template_id": str(template.id),
        "filename": "teamleader-export.xlsx",
        "file_b64": base64.b64encode(content).decode("ascii"),
        "mapping": {
            "Email": "email",
            "First": "first_name",
            "Last": "last_name",
            "DOB": "base_field:date_of_birth",
            "Dest": "case_field:dest_country",
        },
    }


async def test_xlsx_preview_resolves_excel_date_to_iso(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    resp = await imports_client.post(
        "/imports/cases/preview", json=_xlsx_import_body(template), headers=agent_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rows"] == 1
    assert body["create_count"] == 1
    cells = {c["column"]: c["value"] for c in body["rows"][0]["cells"]}
    # The Excel date flowed through as ISO YYYY-MM-DD (no time), end-to-end.
    assert cells["DOB"] == "1990-05-01"


async def test_xlsx_import_creates_dossier_end_to_end(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    resp = await imports_client.post(
        "/imports/cases", json=_xlsx_import_body(template), headers=agent_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["first_name"] == "Zoé"


async def test_corrupt_xlsx_returns_422(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    body = {
        "journey_template_id": str(template.id),
        "filename": "broken.xlsx",
        "file_b64": base64.b64encode(b"not a real xlsx").decode("ascii"),
        "mapping": {"Email": "email", "First": "first_name", "Last": "last_name"},
    }
    resp = await imports_client.post(
        "/imports/cases/preview", json=body, headers=agent_headers(admin)
    )
    assert resp.status_code == 422
