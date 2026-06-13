"""Agency-side journal endpoints: paginated desc, action_type filter,
tenant scoping. (No manual POST — the journal records facts only.)"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase


@pytest.fixture
def activity_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])


@pytest_asyncio.fixture
async def busy_case(
    activity_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> ClientCase:
    """A case with a real activity trail: status change + person add."""
    case = await make_client_case(agency_id=member.agency_id, status="prospect")
    headers = agent_headers(member)
    await activity_client.patch(
        f"/cases/{case.id}", headers=headers, json={"status": "in_progress"}
    )
    await activity_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Lea", "relationship": "spouse"},
    )
    return case


async def test_timeline_paginated_desc(
    activity_client: AsyncClient,
    member: Agent,
    busy_case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    response = await activity_client.get(
        f"/cases/{busy_case.id}/activity?page=1&page_size=1", headers=agent_headers(member)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    # Desc: the most recent action first.
    assert body["items"][0]["action_type"] == "person.added"
    page2 = (
        await activity_client.get(
            f"/cases/{busy_case.id}/activity?page=2&page_size=1",
            headers=agent_headers(member),
        )
    ).json()
    assert page2["items"][0]["action_type"] == "case.status_changed"
    assert page2["items"][0]["details"] == {"old": "prospect", "new": "in_progress"}


async def test_action_type_filter(
    activity_client: AsyncClient,
    member: Agent,
    busy_case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    response = await activity_client.get(
        f"/cases/{busy_case.id}/activity?action_type=case.status_changed",
        headers=agent_headers(member),
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action_type"] == "case.status_changed"


async def test_activity_scoped_and_audience_sealed(
    activity_client: AsyncClient,
    member: Agent,
    busy_case: ClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    foreign = await make_agent(role=system_roles["member"])  # other agency
    assert (
        await activity_client.get(f"/cases/{busy_case.id}/activity", headers=agent_headers(foreign))
    ).status_code == 404
    assert (await activity_client.get(f"/cases/{busy_case.id}/activity")).status_code == 401
