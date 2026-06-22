"""CRM import DRY-RUN preview (validate + report, ZERO write).

Proves the contract: a preview creates NO dossier, queues NO email, opens NO
write transaction, and predicts the per-row outcome with the SAME rules as the
real import — including the RGPD dedup rule (a duplicate is reported only for
the CURRENT agency; a cross-agency email is never revealed).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplate, JourneyTemplateCaseField, JourneyTemplateField
from shared.models.rbac import Role
from src.core import email as email_module
from src.imports.case_import_manager import CaseImportManager
from src.imports.case_import_schema import CaseImportRequest
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep

BASE_MAPPING = {
    "Email": "email",
    "First": "first_name",
    "Last": "last_name",
    "Nat": "base_field:nationality",
    "Dest": "case_field:dest_country",
}


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
    tpl = await make_journey_template(agency_id=admin.agency_id)
    await make_template_step(template=tpl)
    db_session.add(
        JourneyTemplateField(
            template_id=tpl.id,
            kind="base_field",
            reference="nationality",
            required_at_creation=False,
            position=0,
        )
    )
    db_session.add(
        JourneyTemplateCaseField(
            template_id=tpl.id,
            case_field="dest_country",
            required_at_creation=False,
            position=0,
        )
    )
    await db_session.commit()
    return tpl


async def _count_cases(db_session: AsyncSession, agency_id: object) -> int:
    stmt = select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == agency_id)
    return (await db_session.execute(stmt)).scalar_one()


async def _preview(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    template: JourneyTemplate,
    csv: str,
) -> dict:
    body = {"journey_template_id": str(template.id), "mapping": BASE_MAPPING, "csv_text": csv}
    response = await client.post("/imports/cases/preview", json=body, headers=agent_headers(admin))
    assert response.status_code == 200, response.text
    return response.json()


def _statuses(preview: dict) -> dict[int, tuple[str, str | None]]:
    return {r["row"]: (r["status"], r["reason"]) for r in preview["rows"]}


# (1) ZERO write, ZERO email, correct statuses ----------------------------------------


async def test_preview_creates_nothing_sends_nothing_and_predicts_statuses(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    db_session: AsyncSession,
) -> None:
    existing = await make_expat_user(email="known@x.io")
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=existing.id)
    before = await _count_cases(db_session, admin.agency_id)
    email_module.outbox.clear()

    csv = (
        "Email,First,Last,Nat,Dest\n"
        "new@x.io,New,Person,French,PY\n"  # row 1 → create
        "known@x.io,Known,K,French,PY\n"  # row 2 → duplicate_in_agency
        "bad@x.io,Bad,Cell,French,FRA\n"  # row 3 → create_with_errors (FRA ≠ ISO-2)
        ",No,Email,French,PY\n"  # row 4 → rejected missing_email
    )
    preview = await _preview(imports_client, admin, agent_headers, template, csv)

    # NOTHING was written and NOTHING was queued.
    assert await _count_cases(db_session, admin.agency_id) == before
    assert email_module.outbox == []

    assert preview["total_rows"] == 4
    assert preview["create_count"] == 1
    assert preview["create_with_errors_count"] == 1
    assert preview["skipped_count"] == 1
    assert preview["rejected_count"] == 1

    statuses = _statuses(preview)
    assert statuses[1] == ("create", None)
    assert statuses[2] == ("skipped", "duplicate_in_agency")
    assert statuses[3] == ("create_with_errors", None)
    assert statuses[4] == ("rejected", "missing_email")

    # row 3 carries the invalid cell (Dest, ISO-2 country) with its reason; the
    # other cells carry their coerced value.
    row3 = next(r for r in preview["rows"] if r["row"] == 3)
    dest = next(c for c in row3["cells"] if c["column"] == "Dest")
    assert dest["value"] is None and "ISO" in dest["reason"]
    nat = next(c for c in row3["cells"] if c["column"] == "Nat")
    assert nat["value"] == "French" and nat["reason"] is None  # text base field

    # row 1 (create): the dest_country cell is coerced to ISO-2.
    row1 = next(r for r in preview["rows"] if r["row"] == 1)
    assert next(c for c in row1["cells"] if c["column"] == "Dest")["value"] == "PY"


# (2) intra-file duplicate ------------------------------------------------------------


async def test_preview_intra_file_duplicate_second_row_skipped(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    csv = "Email,First,Last,Nat,Dest\ndup@x.io,Dup,One,French,PY\ndup@x.io,Dup,Two,German,US\n"
    preview = await _preview(imports_client, admin, agent_headers, template, csv)
    statuses = _statuses(preview)
    assert statuses[1] == ("create", None)
    assert statuses[2] == ("skipped", "duplicate_in_file")


# (3) RGPD — a cross-agency email is NEVER flagged as a duplicate ---------------------


async def test_preview_cross_agency_email_is_create_not_duplicate(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    db_session: AsyncSession,
) -> None:
    other = await make_agency()
    shared = await make_expat_user(email="shared@x.io", first_name="Shared", last_name="User")
    await make_client_case(agency_id=other.id, principal_expat_user_id=shared.id)
    before = await _count_cases(db_session, admin.agency_id)

    csv = "Email,First,Last,Nat,Dest\nshared@x.io,Shared,User,French,PY\n"
    preview = await _preview(imports_client, admin, agent_headers, template, csv)

    # Predicted create for THIS agency — never reported as a duplicate.
    assert _statuses(preview)[1] == ("create", None)
    assert preview["skipped_count"] == 0
    # Still a strict dry-run: nothing created for the cross-agency email.
    assert await _count_cases(db_session, admin.agency_id) == before


# (4) manager-level proof: preview opens no write, queues no email --------------------


async def test_preview_manager_is_read_only(
    db_session: AsyncSession,
    admin: Agent,
    template: JourneyTemplate,
    rbac_baseline: None,
) -> None:
    csv = "Email,First,Last,Nat,Dest\np1@x.io,P,One,French,PY\np2@x.io,P,Two,German,US\n"
    request = CaseImportRequest(journey_template_id=template.id, mapping=BASE_MAPPING, csv_text=csv)
    email_module.outbox.clear()
    preview = await CaseImportManager(db_session).preview_import(admin, request)

    assert preview.create_count == 2
    assert email_module.outbox == []  # nothing queued
    assert await _count_cases(db_session, admin.agency_id) == 0  # nothing written
