"""Import from an unreferenced CRM ("Autre / CRM générique").

Proves: the engine works with arbitrary CSV headers (no referential); a custom
mapping saves with a reserved slug + free label; save still enforces
target∈parcours and requires the label; strict agency scoping; and an import
resolving a saved custom mapping creates dossiers.
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
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep

MAPPING = {
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
) -> JourneyTemplate:
    template = await make_journey_template(agency_id=agency_id)
    await make_template_step(template=template)
    db_session.add(
        JourneyTemplateField(
            template_id=template.id, kind="base_field", reference="nationality", position=0
        )
    )
    db_session.add(
        JourneyTemplateCaseField(template_id=template.id, case_field="dest_country", position=0)
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


def _custom_body(template: JourneyTemplate, **over: object) -> dict:
    return {
        "journey_template_id": str(template.id),
        "crm_slug": "custom",
        "custom_crm_name": "Mon CRM maison",
        "name": "Config 1",
        "mapping": MAPPING,
        **over,
    }


async def _count_cases(db_session: AsyncSession, agency_id: object) -> int:
    stmt = select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == agency_id)
    return (await db_session.execute(stmt)).scalar_one()


# --- engine independence from the referential ---------------------------------------


async def test_inline_import_with_arbitrary_headers(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    # Headers that exist in NO referential CRM; mapped inline. The engine
    # (parse_csv + validate_cell + create) never consults the referential.
    body = {
        "journey_template_id": str(template.id),
        "mapping": {
            "courriel": "email",
            "prenom": "first_name",
            "nom": "last_name",
            "pays": "case_field:dest_country",
        },
        "csv_text": "courriel,prenom,nom,pays\nz@x.io,Zoé,Zed,PY\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=agent_headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 1
    assert await _count_cases(db_session, admin.agency_id) == 1


# --- saving a custom mapping ---------------------------------------------------------


async def test_save_custom_mapping_then_list(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    created = await imports_client.post("/imports/mappings", json=_custom_body(template), headers=h)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["crm_slug"] == "custom"
    assert body["custom_crm_name"] == "Mon CRM maison"
    assert body["mapping"] == MAPPING

    listed = await imports_client.get("/imports/mappings", params={"crm_slug": "custom"}, headers=h)
    assert listed.status_code == 200
    rows = listed.json()["mappings"]
    assert len(rows) == 1
    assert rows[0]["custom_crm_name"] == "Mon CRM maison"


async def test_save_custom_requires_label(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    body = _custom_body(template, custom_crm_name="")
    response = await imports_client.post(
        "/imports/mappings", json=body, headers=agent_headers(admin)
    )
    assert response.status_code == 422


async def test_save_custom_still_enforces_target_in_parcours(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    # passport_number is not declared on this parcours → 422 even for custom.
    body = _custom_body(template, mapping={**MAPPING, "Pass": "base_field:passport_number"})
    response = await imports_client.post(
        "/imports/mappings", json=body, headers=agent_headers(admin)
    )
    assert response.status_code == 422


# --- strict agency scoping -----------------------------------------------------------


async def test_custom_mapping_scoped_to_agency(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    db_session: AsyncSession,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> None:
    admin_b = await make_agent(role=system_roles["admin"])
    template_b = await _template_with_fields(
        db_session, make_journey_template, make_template_step, admin_b.agency_id
    )
    created_b = await imports_client.post(
        "/imports/mappings", json=_custom_body(template_b), headers=agent_headers(admin_b)
    )
    assert created_b.status_code == 200
    b_id = created_b.json()["id"]

    ha = agent_headers(admin)
    assert (await imports_client.get("/imports/mappings", headers=ha)).json()["mappings"] == []
    assert (await imports_client.delete(f"/imports/mappings/{b_id}", headers=ha)).status_code == 404


# --- import via a saved custom mapping ----------------------------------------------


async def test_import_resolves_saved_custom_mapping_by_id(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    h = agent_headers(admin)
    saved = await imports_client.post("/imports/mappings", json=_custom_body(template), headers=h)
    mapping_id = saved.json()["id"]
    body = {
        "journey_template_id": str(template.id),
        "mapping_id": mapping_id,
        "csv_text": "Email,First,Last,Nat,Dest\ncust@x.io,Cust,Om,French,PY\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=h)
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 1
    assert await _count_cases(db_session, admin.agency_id) == 1
