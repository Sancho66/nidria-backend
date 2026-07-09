"""External provider users (wave A) — restricted agents. The headline is
the FAIL-CLOSED guard: an external authenticates but reaches NOTHING but
its own identity (no scoping yet = no access, by construction). Plus: the
internal listings/owners never include externals, and the two invitation
flows never cross."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.core.rbac.baseline import EXTERNAL_ROLE_NAMES
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def ext_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession, rbac_baseline: None) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def external_agent(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@ext.com"
    )


# --- THE FAIL-CLOSED GUARD (the test that's worth gold) ------------------------------


async def test_external_authenticated_sees_nothing_but_identity(
    ext_client: AsyncClient,
    external_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """An external agent has a valid AGENT token, yet every product route
    — including the permissionless "any agent" ones that zero permissions
    would NOT close — returns 403. Only its own identity is reachable."""
    h = agent_headers(external_agent)
    cid, pid = uuid.uuid4(), uuid.uuid4()  # the guard denies BEFORE resolving the resource

    denied = [
        ("GET", "/agencies/me"),
        ("GET", "/agencies/me/members"),  # the staff list — the leak zero-perm wouldn't close
        ("GET", "/agencies/me/roles"),
        ("GET", "/agencies/me/external-members"),
        ("GET", "/journeys"),
        ("GET", f"/journeys/{uuid.uuid4()}"),
        ("GET", "/message-templates"),
        ("GET", "/cases"),
        ("GET", f"/cases/{cid}"),
        ("GET", f"/cases/{cid}/steps/{pid}/comments"),
        ("GET", f"/cases/{cid}/documents"),
        ("GET", "/dashboard"),
    ]
    for method, path in denied:
        resp = await ext_client.request(method, path, headers=h)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code} (expected 403)"

    # …but the external can see ITSELF (login is useful, nothing leaks).
    me = await ext_client.get("/auth/agent/me", headers=h)
    assert me.status_code == 200
    assert me.json()["id"] == str(external_agent.id)


# --- invite → accept end-to-end ------------------------------------------------------


async def test_external_invite_accept_creates_external_agent(
    ext_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    roles = (await ext_client.get("/agencies/me/external-roles", headers=ah)).json()
    assert len(roles) == 6  # the 6 fixed external roles
    role_id = roles[0]["id"]

    invited = await ext_client.post(
        "/agencies/me/external-invitations",
        headers=ah,
        json={"name": "Notaire Ext", "email": "notaire@ext.com", "role_id": role_id},
    )
    assert invited.status_code == 201, invited.text

    invitation = (
        await db_session.execute(
            select(AgentInvitation).where(AgentInvitation.email == "notaire@ext.com")
        )
    ).scalar_one()
    accepted = await ext_client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "password123",
            "first_name": "Me",
            "last_name": "Robert",
        },
    )
    assert accepted.status_code == 200
    assert "access_token" in accepted.json()

    created = (
        await db_session.execute(
            select(Agent).where(Agent.email == "notaire@ext.com").options(selectinload(Agent.role))
        )
    ).scalar_one()
    assert created.is_external is True  # denormalized flag set
    assert created.role.is_external is True  # wears an external role
    assert created.role.name in EXTERNAL_ROLE_NAMES


# --- anti-regression: internals strictly unchanged -----------------------------------


async def test_members_listing_excludes_externals(
    ext_client: AsyncClient,
    admin: Agent,
    external_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    members = (await ext_client.get("/agencies/me/members", headers=agent_headers(admin))).json()
    ids = {m["id"] for m in members}
    assert str(admin.id) in ids
    assert str(external_agent.id) not in ids  # external never in the internal member list
    assert all(m["is_external"] is False for m in members)


async def test_role_picker_excludes_external_roles(
    ext_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    roles = (await ext_client.get("/agencies/me/roles", headers=agent_headers(admin))).json()
    names = {r["name"] for r in roles}
    assert names.isdisjoint(set(EXTERNAL_ROLE_NAMES))  # no external role in the internal picker


async def test_external_cannot_be_case_owner(
    ext_client: AsyncClient,
    admin: Agent,
    external_agent: Agent,
    agent_headers: AuthHeaders,
) -> None:
    resp = await ext_client.post(
        "/cases",
        headers=agent_headers(admin),
        json={
            "first_name": "Jean",
            "last_name": "Martin",
            "email": "jm@example.com",
            "owner_agent_id": str(external_agent.id),  # external as owner → refused
        },
    )
    assert resp.status_code == 422


async def test_external_not_counted_or_assignable_internally(
    ext_client: AsyncClient,
    admin: Agent,
    external_agent: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    internal = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    # Reassigning an EXTERNAL target via the internal flow → 404 (excluded).
    on_external = await ext_client.put(
        f"/agencies/me/members/{external_agent.id}/role",
        headers=ah,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert on_external.status_code == 404
    # Assigning an EXTERNAL role to an internal agent → 422.
    ext_role_on_internal = await ext_client.put(
        f"/agencies/me/members/{internal.id}/role",
        headers=ah,
        json={"role_id": str(external_role.id)},
    )
    assert ext_role_on_internal.status_code == 422


# --- boundary validation: the two flows never cross ----------------------------------


async def test_internal_invite_rejects_external_role(
    ext_client: AsyncClient,
    admin: Agent,
    external_role: Role,
    agent_headers: AuthHeaders,
) -> None:
    resp = await ext_client.post(
        "/agencies/me/invitations",
        headers=agent_headers(admin),
        json={"email": "x@ext.com", "role_id": str(external_role.id)},
    )
    assert resp.status_code == 422  # external role not invitable via the internal flow


async def test_external_invite_rejects_internal_role(
    ext_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    resp = await ext_client.post(
        "/agencies/me/external-invitations",
        headers=agent_headers(admin),
        json={"name": "X", "email": "y@ext.com", "role_id": str(system_roles["member"].id)},
    )
    assert resp.status_code == 422  # internal role not invitable via the external flow


# --- backfill: existing agents/roles stay internal -----------------------------------


async def test_existing_agents_and_roles_default_internal(
    admin: Agent,
    system_roles: dict[str, Role],
) -> None:
    assert admin.is_external is False
    # The 4 pre-existing internal system roles stay internal (backfill).
    for name in ("admin", "case_manager", "member", "viewer"):
        assert system_roles[name].is_external is False
    # The 6 new ones are external.
    for name in EXTERNAL_ROLE_NAMES:
        assert system_roles[name].is_external is True
