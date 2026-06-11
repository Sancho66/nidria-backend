"""Dashboard: simple counts, agency-scoped, nothing more (V1.5 holds)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase


@pytest.fixture
def dash_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def viewer(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(roles=[system_roles["viewer"]])


async def test_dashboard_counts_scoped(
    dash_client: AsyncClient,
    viewer: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    await make_client_case(agency_id=viewer.agency_id, status="in_progress", dest_country="PY")
    await make_client_case(agency_id=viewer.agency_id, status="in_progress", dest_country="BG")
    await make_client_case(agency_id=viewer.agency_id, status="prospect", dest_country="PY")
    foreign = await make_agent()  # other agency, its cases must not count
    await make_client_case(agency_id=foreign.agency_id, status="closed")

    response = await dash_client.get("/dashboard", headers=agent_headers(viewer))
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "total_cases": 3,
        "by_status": {"in_progress": 2, "prospect": 1},
        "by_dest_country": {"PY": 2, "BG": 1},
    }


async def test_dashboard_requires_case_view(
    dash_client: AsyncClient, make_agent: MakeAgent, agent_headers: AuthHeaders
) -> None:
    roleless = await make_agent()
    assert (await dash_client.get("/dashboard", headers=agent_headers(roleless))).status_code == 403
