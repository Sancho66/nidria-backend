"""GET /cases (list) and /cases/{id} (detail) expose the RESOLVED journey
name (resolve_i18n for the request language), NULL when the case has no
journey. journey_template_id stays alongside for linking."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.journey_plugin import MakeJourneyTemplate


@pytest.fixture
def cases_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])  # holds case.view


async def test_journey_name_in_list_and_detail_and_null(
    cases_client: AsyncClient,
    member: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    template = await make_journey_template(agency_id=member.agency_id, name="Résidence Paraguay")
    with_journey = await make_client_case(
        agency_id=member.agency_id, journey_template_id=template.id
    )
    without = await make_client_case(agency_id=member.agency_id)  # no journey

    # --- list ---
    body = (await cases_client.get("/cases", headers=agent_headers(member))).json()
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(with_journey.id)]["journey_name"] == "Résidence Paraguay"
    assert by_id[str(with_journey.id)]["journey_template_id"] == str(template.id)
    assert by_id[str(without.id)]["journey_name"] is None  # no journey → null, no crash

    # --- detail ---
    d_with = (
        await cases_client.get(f"/cases/{with_journey.id}", headers=agent_headers(member))
    ).json()
    assert d_with["journey_name"] == "Résidence Paraguay"
    d_without = (
        await cases_client.get(f"/cases/{without.id}", headers=agent_headers(member))
    ).json()
    assert d_without["journey_name"] is None


async def test_journey_name_resolved_for_request_language(
    cases_client: AsyncClient,
    member: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    # Scalar = FR anchor; blob carries the EN variant.
    template = await make_journey_template(
        agency_id=member.agency_id,
        name="Parcours FR",
        name_i18n={"fr": "Parcours FR", "en": "Journey EN"},
    )
    case = await make_client_case(agency_id=member.agency_id, journey_template_id=template.id)

    en = (await cases_client.get(f"/cases/{case.id}?lang=en", headers=agent_headers(member))).json()
    assert en["journey_name"] == "Journey EN"  # resolved, not the raw scalar

    fr_list = (await cases_client.get("/cases?lang=fr", headers=agent_headers(member))).json()
    item = next(i for i in fr_list["items"] if i["id"] == str(case.id))
    assert item["journey_name"] == "Parcours FR"
