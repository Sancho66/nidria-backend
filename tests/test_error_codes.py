"""Error i18n wave 1 (point 9) — stable codes + params on the error envelope.

The envelope is {"detail": <english, unchanged>, "code": <stable code>,
"params": {...}}. Wave 1 assigns specific codes to the imports / journeys /
cases domains; everything else keeps the class CATEGORY as code (the
pre-i18n behaviour). `detail` stays byte-identical — the existing
assertions across the suite are the compat proof.

Covers: the category fallback on an unmigrated domain, migrated codes with
empty params, a parameterized code, the aggregated import.mapping_invalid
with its structured token lists, and the parse_sorts wrap."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate


@pytest.fixture
def ec_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- the category fallback (unmigrated domains) ---------------------------------------


async def test_unmigrated_domain_keeps_category_code(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The activity domain is NOT in wave 1: same english detail as the
    migrated cases domain, but the code is still the class category."""
    r = await ec_client.get(f"/cases/{uuid.uuid4()}/activity", headers=agent_headers(admin))
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Case not found."
    assert body["code"] == "not_found"  # category, not "case.not_found"
    assert body["params"] == {}


async def test_missing_token_keeps_category_code(ec_client: AsyncClient) -> None:
    r = await ec_client.get("/cases")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "unauthorized"
    assert body["params"] == {}


# --- migrated codes --------------------------------------------------------------------


async def test_case_not_found_code(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    r = await ec_client.get(f"/cases/{uuid.uuid4()}", headers=agent_headers(admin))
    assert r.status_code == 404
    assert r.json() == {"detail": "Case not found.", "code": "case.not_found", "params": {}}


async def test_journey_template_not_found_code(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    r = await ec_client.get(f"/journeys/{uuid.uuid4()}", headers=agent_headers(admin))
    assert r.status_code == 404
    assert r.json()["code"] == "journey.template_not_found"


async def test_sort_invalid_code(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    r = await ec_client.get(
        "/cases", params={"sort_by": "bogus", "order": "asc"}, headers=agent_headers(admin)
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "case.sort_invalid"
    assert "Unknown sort field" in body["detail"]  # english detail kept for logs


async def test_requirement_field_not_declared_carries_params(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = (await ec_client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (
        await ec_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S"})
    ).json()["id"]
    # base_field requested WITHOUT being declared in the Informations tab.
    r = await ec_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "journey.requirement_field_not_declared"
    assert body["params"] == {"reference": "passport_number"}
    assert body["detail"].startswith("The field 'passport_number' must first be added")


# --- the aggregated import.mapping_invalid (Eric's capture) ----------------------------


async def test_import_mapping_invalid_structured_params(
    ec_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """ONE code for the aggregate, the token lists as params — always all
    three keys, empty ones included. The english detail keeps the readable
    aggregation for logs."""
    template: JourneyTemplate = await make_journey_template(agency_id=admin.agency_id)
    r = await ec_client.post(
        "/imports/mappings",
        headers=agent_headers(admin),
        json={
            "journey_template_id": str(template.id),
            "crm_slug": "hubspot-crm",
            "name": "Broken",
            "mapping": {
                "Email": "email",
                "Dup": "email",  # identity mapped twice
                "Junk": "junk",  # unparseable token
                "Nat": "base_field:nationality",  # not declared in Informations
            },
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "import.mapping_invalid"
    assert body["params"] == {
        "unparseable": ["junk"],
        "undeclared": ["base_field:nationality"],
        "duplicated": ["identity:email"],
    }
    assert body["detail"].startswith("Invalid mapping — ")
    assert "targets not declared in the parcours Informations tab" in body["detail"]


async def test_import_mapping_invalid_import_stage_params(
    ec_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """The import-time stage layers its own keys onto the SAME code:
    missing CSV columns / unmapped identity."""
    template: JourneyTemplate = await make_journey_template(agency_id=admin.agency_id)
    r = await ec_client.post(
        "/imports/cases",
        headers=agent_headers(admin),
        json={
            "journey_template_id": str(template.id),
            "csv_text": "Email\nmartin@example.com\n",
            # First/Last identity targets unmapped + a column absent from the CSV.
            "mapping": {"Email": "email", "Ghost": "first_name"},
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "import.mapping_invalid"
    assert body["params"] == {
        "missing_columns": ["Ghost"],
        "missing_identity": ["last_name"],
    }
