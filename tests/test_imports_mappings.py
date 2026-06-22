"""Saved CRM import mappings (BLOC 3) — CRUD, target∈parcours validation at
save, STRICT agency scoping (A never sees B), and the import resolving a saved
mapping (by crm_slug and by id)."""

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

CRM_SLUG = "hubspot-crm"  # a real, usable referential slug
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


def _upsert_body(template: JourneyTemplate, **over: object) -> dict:
    return {
        "journey_template_id": str(template.id),
        "crm_slug": CRM_SLUG,
        "name": "HubSpot → Default",
        "mapping": MAPPING,
        **over,
    }


# --- CRUD --------------------------------------------------------------------------------


async def test_create_then_list_and_resolve(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    created = await imports_client.post("/imports/mappings", json=_upsert_body(template), headers=h)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["crm_slug"] == CRM_SLUG
    assert body["mapping"] == MAPPING

    listed = await imports_client.get("/imports/mappings", headers=h)
    assert listed.status_code == 200
    assert len(listed.json()["mappings"]) == 1

    resolved = await imports_client.get(
        "/imports/mappings/resolve",
        params={"journey_template_id": str(template.id), "crm_slug": CRM_SLUG},
        headers=h,
    )
    assert resolved.status_code == 200
    assert resolved.json()["id"] == body["id"]


async def test_two_different_names_coexist(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    # Two DIFFERENT names for the same (parcours, CRM) → TWO rows coexist.
    h = agent_headers(admin)
    a = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template, name="test1"), headers=h
    )
    b = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template, name="test2"), headers=h
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    assert a.json()["id"] != b.json()["id"]
    listed = await imports_client.get("/imports/mappings", headers=h)
    assert {m["name"] for m in listed.json()["mappings"]} == {"test1", "test2"}
    assert len(listed.json()["mappings"]) == 2


async def test_same_name_create_conflicts_409(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    # Same name twice (a CREATE, no id) → 409, never a silent overwrite.
    h = agent_headers(admin)
    first = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template, name="test1"), headers=h
    )
    assert first.status_code == 200
    dup = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template, name="test1"), headers=h
    )
    assert dup.status_code == 409, dup.text
    listed = await imports_client.get("/imports/mappings", headers=h)
    assert len(listed.json()["mappings"]) == 1


async def test_edit_by_id_updates_in_place(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    # An EDIT (id present) updates THAT row — name kept, mapping replaced.
    h = agent_headers(admin)
    created = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template, name="test1"), headers=h
    )
    cid = created.json()["id"]
    edited = await imports_client.post(
        "/imports/mappings",
        json=_upsert_body(template, id=cid, name="test1", mapping={"Email": "email"}),
        headers=h,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["id"] == cid
    assert edited.json()["mapping"] == {"Email": "email"}
    listed = await imports_client.get("/imports/mappings", headers=h)
    assert len(listed.json()["mappings"]) == 1


async def test_delete_mapping(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    created = await imports_client.post("/imports/mappings", json=_upsert_body(template), headers=h)
    mapping_id = created.json()["id"]
    deleted = await imports_client.delete(f"/imports/mappings/{mapping_id}", headers=h)
    assert deleted.status_code == 204
    listed = await imports_client.get("/imports/mappings", headers=h)
    assert listed.json()["mappings"] == []


# --- validation cible ∈ parcours (reused import check) -----------------------------------


async def test_save_rejects_target_outside_parcours(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    body = _upsert_body(template, mapping={**MAPPING, "Pass": "base_field:passport_number"})
    response = await imports_client.post(
        "/imports/mappings", json=body, headers=agent_headers(admin)
    )
    assert response.status_code == 422


async def test_save_rejects_unknown_crm_slug(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    body = _upsert_body(template, crm_slug="not-a-real-crm")
    response = await imports_client.post(
        "/imports/mappings", json=body, headers=agent_headers(admin)
    )
    assert response.status_code == 422


# --- STRICT agency scoping (RGPD) --------------------------------------------------------


async def test_agency_a_cannot_see_agency_b_mapping(
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
    # Agency B saves a mapping for its OWN template.
    admin_b = await make_agent(role=system_roles["admin"])
    template_b = await _template_with_fields(
        db_session, make_journey_template, make_template_step, admin_b.agency_id
    )
    created_b = await imports_client.post(
        "/imports/mappings", json=_upsert_body(template_b), headers=agent_headers(admin_b)
    )
    assert created_b.status_code == 200
    b_mapping_id = created_b.json()["id"]

    ha = agent_headers(admin)  # agency A
    # A's list never contains B's mapping
    a_list = await imports_client.get("/imports/mappings", headers=ha)
    assert a_list.json()["mappings"] == []
    # A resolving B's (template, crm) → 404 (scoped, no leak)
    a_resolve = await imports_client.get(
        "/imports/mappings/resolve",
        params={"journey_template_id": str(template_b.id), "crm_slug": CRM_SLUG},
        headers=ha,
    )
    assert a_resolve.status_code == 404
    # A deleting B's mapping by id → 404 (never reaches another agency's row)
    a_delete = await imports_client.delete(f"/imports/mappings/{b_mapping_id}", headers=ha)
    assert a_delete.status_code == 404
    # B's mapping is still intact
    b_list = await imports_client.get("/imports/mappings", headers=agent_headers(admin_b))
    assert len(b_list.json()["mappings"]) == 1


# --- import resolves a saved mapping -----------------------------------------------------


async def test_import_resolves_saved_mapping_by_crm_slug(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    h = agent_headers(admin)
    await imports_client.post("/imports/mappings", json=_upsert_body(template), headers=h)
    # No inline mapping — only the crm_slug reference.
    body = {
        "journey_template_id": str(template.id),
        "crm_slug": CRM_SLUG,
        "csv_text": "Email,First,Last,Nat,Dest\nsaved@x.io,Saved,User,French,PY\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=h)
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 1
    count_stmt = (
        select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == admin.agency_id)
    )
    assert (await db_session.execute(count_stmt)).scalar_one() == 1


async def test_import_resolves_saved_mapping_by_id(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    saved = await imports_client.post("/imports/mappings", json=_upsert_body(template), headers=h)
    mapping_id = saved.json()["id"]
    body = {
        "journey_template_id": str(template.id),
        "mapping_id": mapping_id,
        "csv_text": "Email,First,Last,Nat,Dest\nbyid@x.io,By,Id,German,US\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=h)
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 1


async def test_import_without_any_mapping_source_is_422(
    imports_client: AsyncClient, admin: Agent, template: JourneyTemplate, agent_headers: AuthHeaders
) -> None:
    body = {
        "journey_template_id": str(template.id),
        "csv_text": "Email,First,Last\nx@x.io,X,Y\n",
    }
    response = await imports_client.post("/imports/cases", json=body, headers=agent_headers(admin))
    assert response.status_code == 422
