"""CRM referential endpoints (BLOC 1) — auth, permission, payload, 404.

The import.manage gate: admin holds it, case_manager and a no-perm agent
do not. Deny-by-default + audience checks are exercised here.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def imports_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def test_list_crms_ok_for_admin(
    imports_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await imports_client.get("/imports/crms", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    # 29 in the source, 18 served (>= MIN_USABLE_FIELDS headers)
    assert len(body["crms"]) == 18
    slugs = {c["slug"] for c in body["crms"]}
    assert {"hubspot-crm", "pipedrive"} <= slugs
    assert "actionstep" not in slugs  # hidden, below threshold
    hubspot = next(c for c in body["crms"] if c["slug"] == "hubspot-crm")
    assert hubspot["field_count"] == 15


async def test_get_crm_detail_ok(
    imports_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await imports_client.get("/imports/crms/hubspot-crm", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "hubspot-crm"
    headers_by_csv = {h["csv"]: h for h in body["headers"]}
    assert headers_by_csv["Email"]["format"] == "email"
    assert headers_by_csv["Email"]["dedup"] is True
    assert all(h["csv"] != "" for h in body["headers"])


async def test_get_unknown_crm_404(
    imports_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await imports_client.get("/imports/crms/nope", headers=agent_headers(admin))
    assert response.status_code == 404


async def test_missing_token_401(imports_client: AsyncClient) -> None:
    response = await imports_client.get("/imports/crms")
    assert response.status_code == 401


async def test_agent_without_permission_403(
    imports_client: AsyncClient, agent: Agent, agent_headers: AuthHeaders
) -> None:
    # the default agent has an empty custom role → no import.manage
    response = await imports_client.get("/imports/crms", headers=agent_headers(agent))
    assert response.status_code == 403


async def test_case_manager_allowed_by_default(
    imports_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # case_manager holds import.manage by default (bulk onboarding is case
    # work) — locks the matrix decision; an agency can still revoke in data.
    case_manager = await make_agent(role=system_roles["case_manager"])
    response = await imports_client.get("/imports/crms", headers=agent_headers(case_manager))
    assert response.status_code == 200
