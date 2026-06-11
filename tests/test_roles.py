"""Step 18 battery: role & permission management surface — delegation
ceiling on both paths (matrix and assignment), system-role
immutability, anti-lockout, self-mutation ban, declarative sets,
duplicate-clones-matrix, tenant scoping, immediate effect."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role
from src.core.rbac.baseline import SYSTEM_ROLE_MATRIX
from src.core.rbac.permissions import Permission
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.rbac_plugin import MakeRole

# A realistic mid-level ceiling: can manage agents and roles, owns the
# case-work permissions, but NOT agency.manage / job.manage /
# note.view_confidential — the gap the ceiling tests probe.
LIMITED_CEILING = (
    Permission.AGENT_MANAGE,
    Permission.ROLE_MANAGE,
    Permission.CASE_VIEW,
    Permission.CASE_EDIT,
    Permission.STEP_COMPLETE,
    Permission.REMINDER_CREATE,
    Permission.REMINDER_APPROVE,
    Permission.DOCUMENT_VALIDATE,
)


@pytest.fixture
def roles_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def perm_ids(rbac_baseline: None, db_session: AsyncSession) -> dict[str, str]:
    rows = (await db_session.execute(select(PermissionRow))).scalars().all()
    return {row.key: str(row.id) for row in rows}


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(roles=[system_roles["admin"]])


@pytest_asyncio.fixture
async def limited_manager(
    make_agency: MakeAgency, make_agent: MakeAgent, make_role: MakeRole
) -> Agent:
    agency = await make_agency()
    role = await make_role(
        permissions=list(LIMITED_CEILING), agency_id=agency.id, name="ops-manager"
    )
    return await make_agent(agency_id=agency.id, roles=[role])


# --- GET /permissions ------------------------------------------------------------


async def test_permissions_catalogue(
    roles_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await roles_client.get("/permissions", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert {p["key"] for p in body} == {p.value for p in Permission}
    sample = next(p for p in body if p["key"] == "case.view")
    assert sample["category"] == "case"
    assert sample["label"]


async def test_permissions_catalogue_requires_role_manage(
    roles_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(roles=[system_roles["member"]])
    response = await roles_client.get("/permissions", headers=agent_headers(member))
    assert response.status_code == 403


async def test_case_manager_locked_out_of_rbac_surface(
    roles_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The guinea pig: case_manager holds neither role.manage nor
    agent.manage — the whole surface must be enforcement-denied."""
    case_manager = await make_agent(roles=[system_roles["case_manager"]])
    colleague = await make_agent(agency_id=case_manager.agency_id)
    headers = agent_headers(case_manager)
    assert (await roles_client.get("/permissions", headers=headers)).status_code == 403
    create = await roles_client.post(
        "/agencies/me/roles", headers=headers, json={"name": "x", "permission_ids": []}
    )
    assert create.status_code == 403
    assign = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/roles", headers=headers, json={"role_ids": []}
    )
    assert assign.status_code == 403


# --- custom role CRUD ------------------------------------------------------------


async def test_create_custom_role(
    roles_client: AsyncClient, admin: Agent, perm_ids: dict[str, str], agent_headers: AuthHeaders
) -> None:
    response = await roles_client.post(
        "/agencies/me/roles",
        headers=agent_headers(admin),
        json={
            "name": "doc-checker",
            "permission_ids": [perm_ids["case.view"], perm_ids["document.validate"]],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "doc-checker"
    assert body["is_system"] is False
    assert [p["key"] for p in body["permissions"]] == ["case.view", "document.validate"]


async def test_create_role_beyond_ceiling_403_names_missing(
    roles_client: AsyncClient,
    limited_manager: Agent,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    response = await roles_client.post(
        "/agencies/me/roles",
        headers=agent_headers(limited_manager),
        json={
            "name": "too-big",
            "permission_ids": [
                perm_ids["case.view"],
                perm_ids["job.manage"],
                perm_ids["agency.manage"],
            ],
        },
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "agency.manage" in detail and "job.manage" in detail
    assert "case.view" not in detail  # only the offending permissions are named


async def test_set_role_permissions_declarative(
    roles_client: AsyncClient,
    admin: Agent,
    make_role: MakeRole,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    response = await roles_client.put(
        f"/agencies/me/roles/{role.id}/permissions",
        headers=agent_headers(admin),
        json={"permission_ids": [perm_ids["case.edit"], perm_ids["step.complete"]]},
    )
    assert response.status_code == 200
    # Declarative set: case.view is gone, exactly the new pair remains.
    assert {p["key"] for p in response.json()["permissions"]} == {"case.edit", "step.complete"}


async def test_rename_conflict_409(
    roles_client: AsyncClient, admin: Agent, make_role: MakeRole, agent_headers: AuthHeaders
) -> None:
    await make_role(permissions=[], agency_id=admin.agency_id, name="taken")
    role = await make_role(permissions=[], agency_id=admin.agency_id, name="orig")
    response = await roles_client.patch(
        f"/agencies/me/roles/{role.id}", headers=agent_headers(admin), json={"name": "taken"}
    )
    assert response.status_code == 409


async def test_unknown_ids_422(
    roles_client: AsyncClient, admin: Agent, make_agent: MakeAgent, agent_headers: AuthHeaders
) -> None:
    ghost = str(uuid.uuid4())
    create = await roles_client.post(
        "/agencies/me/roles",
        headers=agent_headers(admin),
        json={"name": "x", "permission_ids": [ghost]},
    )
    assert create.status_code == 422
    assert ghost in create.json()["detail"]

    colleague = await make_agent(agency_id=admin.agency_id)
    assign = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/roles",
        headers=agent_headers(admin),
        json={"role_ids": [ghost]},
    )
    assert assign.status_code == 422


async def test_system_role_mutations_403(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    member_role = system_roles["member"]
    headers = agent_headers(admin)
    attempts = [
        ("PATCH", f"/agencies/me/roles/{member_role.id}", {"name": "renamed"}),
        (
            "PUT",
            f"/agencies/me/roles/{member_role.id}/permissions",
            {"permission_ids": [perm_ids["case.view"]]},
        ),
        ("DELETE", f"/agencies/me/roles/{member_role.id}", None),
    ]
    for method, url, payload in attempts:
        response = await roles_client.request(method, url, headers=headers, json=payload)
        assert response.status_code == 403, (method, url)
        assert "system roles" in response.json()["detail"].lower()


async def test_delete_assigned_role_409_then_free_role_ok(
    roles_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    assigned = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    await make_agent(agency_id=admin.agency_id, roles=[assigned])
    response = await roles_client.delete(
        f"/agencies/me/roles/{assigned.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 409
    assert "assigned to 1 agent(s)" in response.json()["detail"]

    free = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    response = await roles_client.delete(
        f"/agencies/me/roles/{free.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 200
    listing = await roles_client.get("/agencies/me/roles", headers=agent_headers(admin))
    assert str(free.id) not in {r["id"] for r in listing.json()}


# --- duplicate -------------------------------------------------------------------


async def test_duplicate_system_role_clones_matrix(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    source = system_roles["case_manager"]
    response = await roles_client.post(
        f"/agencies/me/roles/{source.id}/duplicate",
        headers=agent_headers(admin),
        json={"name": "case-manager-custom"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["is_system"] is False
    expected = {p.value for p in SYSTEM_ROLE_MATRIX["case_manager"]}
    assert {p["key"] for p in body["permissions"]} == expected


async def test_duplicate_beyond_ceiling_403(
    roles_client: AsyncClient,
    limited_manager: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The copy bypass: duplicating an oversized role would mint
    permissions the actor does not hold."""
    response = await roles_client.post(
        f"/agencies/me/roles/{system_roles['admin'].id}/duplicate",
        headers=agent_headers(limited_manager),
        json={"name": "shadow-admin"},
    )
    assert response.status_code == 403
    assert "ceiling" in response.json()["detail"].lower()


# --- member role assignment --------------------------------------------------------


async def test_assign_role_beyond_ceiling_403(
    roles_client: AsyncClient,
    limited_manager: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Without ceiling (b) the bypass is: create an inflated role and
    assign it — or here, directly assign the system admin role."""
    colleague = await make_agent(agency_id=limited_manager.agency_id)
    response = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/roles",
        headers=agent_headers(limited_manager),
        json={"role_ids": [str(system_roles["admin"].id)]},
    )
    assert response.status_code == 403
    assert "ceiling" in response.json()["detail"].lower()


async def test_set_own_roles_403(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    response = await roles_client.put(
        f"/agencies/me/members/{admin.id}/roles",
        headers=agent_headers(admin),
        json={"role_ids": [str(system_roles["admin"].id)]},
    )
    assert response.status_code == 403


async def test_assignment_takes_effect_immediately(
    roles_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """No cache anywhere: the very next request runs with the new set."""
    target = await make_agent(agency_id=admin.agency_id, roles=[system_roles["viewer"]])
    before = await roles_client.get("/auth/agent/me", headers=agent_headers(target))
    assert "case.edit" not in before.json()["effective_permissions"]

    response = await roles_client.put(
        f"/agencies/me/members/{target.id}/roles",
        headers=agent_headers(admin),
        json={"role_ids": [str(system_roles["case_manager"].id)]},
    )
    assert response.status_code == 200
    assert response.json()["roles"] == ["case_manager"]

    after = await roles_client.get("/auth/agent/me", headers=agent_headers(target))
    assert "case.edit" in after.json()["effective_permissions"]


# --- anti-lockout ----------------------------------------------------------------


async def test_demote_admin_ok_when_actor_keeps_management(
    roles_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Demoting the only admin ROLE holder is fine when another agent
    carries agent.manage via a custom role (the capability counts, not
    the role)."""
    agency = await make_agency()
    sole_admin = await make_agent(agency_id=agency.id, roles=[system_roles["admin"]])
    custom_manager = await make_role(
        permissions=list(LIMITED_CEILING), agency_id=agency.id, name="ops"
    )
    actor = await make_agent(agency_id=agency.id, roles=[custom_manager])
    response = await roles_client.put(
        f"/agencies/me/members/{sole_admin.id}/roles",
        headers=agent_headers(actor),
        json={"role_ids": [str(system_roles["viewer"].id)]},
    )
    assert response.status_code == 200


async def test_lockout_via_custom_matrix_edit_409(
    roles_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """The reachable lockout vector: the agency's ONLY agent.manage
    holder carries it through a custom role and edits that role's
    matrix, dropping agent.manage — refused, the agency would be
    unrecoverable."""
    agency = await make_agency()
    solo_manager_role = await make_role(
        permissions=[Permission.AGENT_MANAGE, Permission.ROLE_MANAGE, Permission.CASE_VIEW],
        agency_id=agency.id,
        name="solo-manager",
    )
    actor = await make_agent(agency_id=agency.id, roles=[solo_manager_role])
    response = await roles_client.put(
        f"/agencies/me/roles/{solo_manager_role.id}/permissions",
        headers=agent_headers(actor),
        json={"permission_ids": [perm_ids["role.manage"], perm_ids["case.view"]]},
    )
    assert response.status_code == 409
    assert "without any manager" in response.json()["detail"]


async def test_matrix_edit_keeping_agent_manage_ok(
    roles_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency()
    solo_manager_role = await make_role(
        permissions=[Permission.AGENT_MANAGE, Permission.ROLE_MANAGE, Permission.CASE_VIEW],
        agency_id=agency.id,
        name="solo-manager",
    )
    actor = await make_agent(agency_id=agency.id, roles=[solo_manager_role])
    response = await roles_client.put(
        f"/agencies/me/roles/{solo_manager_role.id}/permissions",
        headers=agent_headers(actor),
        json={"permission_ids": [perm_ids["agent.manage"], perm_ids["role.manage"]]},
    )
    assert response.status_code == 200


# --- tenant scoping --------------------------------------------------------------


async def test_cross_agency_scoping(
    roles_client: AsyncClient,
    admin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    other_agency = await make_agency()
    foreign_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=other_agency.id)
    foreign_agent = await make_agent(agency_id=other_agency.id)
    headers = agent_headers(admin)

    # Mutating a foreign custom role: 404, no existence leak.
    rename = await roles_client.patch(
        f"/agencies/me/roles/{foreign_role.id}", headers=headers, json={"name": "x"}
    )
    assert rename.status_code == 404

    # Assigning a foreign custom role to an own colleague: 422.
    colleague = await make_agent(agency_id=admin.agency_id)
    assign_foreign_role = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/roles",
        headers=headers,
        json={"role_ids": [str(foreign_role.id)]},
    )
    assert assign_foreign_role.status_code == 422

    # Setting roles of an agent from another agency: 404.
    assign_foreign_agent = await roles_client.put(
        f"/agencies/me/members/{foreign_agent.id}/roles",
        headers=headers,
        json={"role_ids": []},
    )
    assert assign_foreign_agent.status_code == 404
