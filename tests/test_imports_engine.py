"""CRM case import engine (BLOC 2) — the transactional creator.

Proves the frozen rules: 3 valid → 3 dossiers; missing email → rejected;
in-agency duplicate → skipped; cross-agency email → created with NO leak;
invalid cell → created without that field; intra-file duplicate → 2nd
skipped (no 500); emails decoupled (manager sends nothing synchronously).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
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


async def _template_with_fields(
    db_session: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    agency_id: object,
    *,
    nationality_required: bool = False,
) -> JourneyTemplate:
    template = await make_journey_template(agency_id=agency_id)
    await make_template_step(template=template)
    db_session.add(
        JourneyTemplateField(
            template_id=template.id,
            kind="base_field",
            reference="nationality",
            required_at_creation=nationality_required,
            position=0,
        )
    )
    db_session.add(
        JourneyTemplateCaseField(
            template_id=template.id,
            case_field="dest_country",
            required_at_creation=False,
            position=0,
        )
    )
    await db_session.commit()
    return template


@pytest_asyncio.fixture
async def template(
    db_session: AsyncSession,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> JourneyTemplate:
    return await _template_with_fields(
        db_session, make_journey_template, make_template_step, admin.agency_id
    )


async def _count_cases(db_session: AsyncSession, agency_id: object) -> int:
    stmt = select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == agency_id)
    return (await db_session.execute(stmt)).scalar_one()


async def _import(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    template: JourneyTemplate,
    csv: str,
) -> dict:
    body = {
        "journey_template_id": str(template.id),
        "mapping": BASE_MAPPING,
        "csv_text": csv,
    }
    response = await client.post("/imports/cases", json=body, headers=agent_headers(admin))
    assert response.status_code == 200, response.text
    return response.json()


# (a) ----------------------------------------------------------------------------------


async def test_three_valid_rows_create_three_cases(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    csv = (
        "Email,First,Last,Nat,Dest\n"
        "a@x.io,Alice,A,French,PY\n"
        "b@x.io,Bob,B,German,US\n"
        "c@x.io,Carol,C,Spanish,ES\n"
    )
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["total_rows"] == 3
    assert report["created_count"] == 3
    assert report["skipped_count"] == 0
    assert report["rejected_count"] == 0
    assert await _count_cases(db_session, admin.agency_id) == 3


# (b) ----------------------------------------------------------------------------------


async def test_row_without_email_is_rejected(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    csv = "Email,First,Last,Nat,Dest\n,Dan,D,Italian,IT\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 0
    assert report["rejected_count"] == 1
    assert report["rejected"][0]["reason"] == "missing_email"
    assert await _count_cases(db_session, admin.agency_id) == 0


# (c) ----------------------------------------------------------------------------------


async def test_email_already_agency_client_is_skipped(
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

    csv = "Email,First,Last,Nat,Dest\nknown@x.io,Known,K,French,PY\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 0
    assert report["skipped_count"] == 1
    assert report["skipped"][0]["reason"] == "duplicate_in_agency"
    # no second dossier was created for this email
    assert await _count_cases(db_session, admin.agency_id) == before


# (d) — RGPD: cross-agency email must NOT be flagged, created normally ------------------


async def test_email_at_other_agency_is_created_without_leak(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    make_agency: MakeAgency,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    db_session: AsyncSession,
) -> None:
    other_agency = await make_agency()
    shared = await make_expat_user(email="shared@x.io", first_name="Shared", last_name="User")
    await make_client_case(agency_id=other_agency.id, principal_expat_user_id=shared.id)

    csv = "Email,First,Last,Nat,Dest\nshared@x.io,Shared,User,French,PY\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)

    # created normally for THIS agency — never reported as a duplicate
    assert report["created_count"] == 1
    assert report["skipped_count"] == 0
    assert report["rejected_count"] == 0
    # the report carries no cross-agency reference (structurally: row + case_id
    # + the principal's own name (from THIS agency's CSV) + field_errors)
    created = report["created"][0]
    assert set(created.keys()) == {"row", "case_id", "first_name", "last_name", "field_errors"}
    assert created["first_name"] == "Shared" and created["last_name"] == "User"
    # the new dossier reuses the SAME shared expat identity (link-or-create)
    stmt = select(ClientCase).where(ClientCase.agency_id == admin.agency_id)
    case = (await db_session.execute(stmt)).scalar_one()
    assert case.principal_expat_user_id == shared.id


# (e) ----------------------------------------------------------------------------------


async def test_invalid_cell_creates_case_and_reports_field_error(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    # Dest "FRA" is not ISO-2 → non-blocking field error; dossier still created.
    csv = "Email,First,Last,Nat,Dest\ne@x.io,Eve,E,French,FRA\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 1
    created = report["created"][0]
    assert len(created["field_errors"]) == 1
    err = created["field_errors"][0]
    assert err["column"] == "Dest"
    assert err["target"] == "case_field:dest_country"
    assert "ISO" in err["reason"]
    # the dossier exists with the VALID field (nationality) set and the bad
    # field (dest_country) left unset
    stmt = select(ClientCase).where(ClientCase.agency_id == admin.agency_id)
    case = (await db_session.execute(stmt)).scalar_one()
    assert case.dest_country is None
    person_stmt = select(ExpatUser).where(ExpatUser.email == "e@x.io")
    assert (await db_session.execute(person_stmt)).scalar_one() is not None


# (f) ----------------------------------------------------------------------------------


async def test_intra_file_duplicate_second_row_skipped_no_500(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    csv = "Email,First,Last,Nat,Dest\ndup@x.io,Dup,One,French,PY\ndup@x.io,Dup,Two,German,US\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 1
    assert report["skipped_count"] == 1
    assert report["skipped"][0]["reason"] == "duplicate_in_file"
    # exactly one dossier for the duplicated email
    assert await _count_cases(db_session, admin.agency_id) == 1


# (g) — emails are NOT sent synchronously in the request -------------------------------


async def test_emails_are_decoupled_manager_sends_nothing(
    db_session: AsyncSession,
    admin: Agent,
    template: JourneyTemplate,
    rbac_baseline: None,
) -> None:
    csv = "Email,First,Last,Nat,Dest\ng1@x.io,G,One,French,PY\ng2@x.io,G,Two,German,US\n"
    request = CaseImportRequest(journey_template_id=template.id, mapping=BASE_MAPPING, csv_text=csv)
    email_module.outbox.clear()
    report, pending = await CaseImportManager(db_session).run_import(admin, request)

    assert report.created_count == 2
    # the manager sent ZERO emails synchronously — they are deferred…
    assert email_module.outbox == []
    # …and handed back for the router to dispatch out of band (one per dossier)
    assert len(pending) == 2
    assert {p.to for p in pending} == {"g1@x.io", "g2@x.io"}


# bonus — pre-flight refus + required-field rule ---------------------------------------


async def test_mapping_target_outside_parcours_is_refused(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
) -> None:
    body = {
        "journey_template_id": str(template.id),
        # passport_number is NOT declared in this template → refus before import
        "mapping": {**BASE_MAPPING, "Pass": "base_field:passport_number"},
        "csv_text": "Email,First,Last,Nat,Dest,Pass\na@x.io,A,A,French,PY,X\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=agent_headers(admin))
    assert response.status_code == 422


async def test_required_field_missing_rejects_row(
    imports_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> None:
    template = await _template_with_fields(
        db_session,
        make_journey_template,
        make_template_step,
        admin.agency_id,
        nationality_required=True,
    )
    # nationality is required but empty on the row → row rejected, no dossier
    csv = "Email,First,Last,Nat,Dest\nr@x.io,R,R,,PY\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 0
    assert report["rejected_count"] == 1
    rejected = report["rejected"][0]
    assert rejected["reason"] == "missing_required_fields"
    assert "base_field:nationality" in rejected["details"]
    assert await _count_cases(db_session, admin.agency_id) == 0
