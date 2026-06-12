"""RBAC management battery, single-role model (Prism): delegation
ceiling on both paths, copy-on-write system roles (clone + rebind,
masking in listing AND assignment, delete = unmask), anti-lockout,
self-mutation ban, tenant scoping, migration backfill semantics."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
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
# note.view_confidential - the gap the ceiling tests probe.
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
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def limited_manager(
    make_agency: MakeAgency, make_agent: MakeAgent, make_role: MakeRole
) -> Agent:
    agency = await make_agency()
    role = await make_role(
        permissions=list(LIMITED_CEILING), agency_id=agency.id, name="ops-manager"
    )
    return await make_agent(agency_id=agency.id, role=role)


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
    member = await make_agent(role=system_roles["member"])
    response = await roles_client.get("/permissions", headers=agent_headers(member))
    assert response.status_code == 403


async def test_case_manager_locked_out_of_rbac_surface(
    roles_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The guinea pig: case_manager holds neither role.manage nor
    agent.manage - the whole surface must be enforcement-denied."""
    case_manager = await make_agent(role=system_roles["case_manager"])
    colleague = await make_agent(agency_id=case_manager.agency_id)
    headers = agent_headers(case_manager)
    assert (await roles_client.get("/permissions", headers=headers)).status_code == 403
    create = await roles_client.post(
        "/agencies/me/roles", headers=headers, json={"name": "x", "permission_ids": []}
    )
    assert create.status_code == 403
    assign = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(uuid.uuid4())},
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
    assert body["cloned_from_role_id"] is None
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
        f"/agencies/me/members/{colleague.id}/role",
        headers=agent_headers(admin),
        json={"role_id": ghost},
    )
    assert assign.status_code == 422


async def test_delete_assigned_custom_role_409_then_free_role_ok(
    roles_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    assigned = await make_role(permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id)
    await make_agent(agency_id=admin.agency_id, role=assigned)
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


async def test_delete_system_role_403(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    response = await roles_client.delete(
        f"/agencies/me/roles/{system_roles['member'].id}", headers=agent_headers(admin)
    )
    assert response.status_code == 403
    assert "system roles" in response.json()["detail"].lower()


# --- copy-on-write ----------------------------------------------------------------


async def test_cow_edit_system_role_clones_and_rebinds(
    roles_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """Editing a system role never touches it: the agency gets a custom
    clone with the edit applied, and its wearers are rebound."""
    member_role = system_roles["member"]
    wearer = await make_agent(agency_id=admin.agency_id, role=member_role)
    member_keys = {p.value for p in SYSTEM_ROLE_MATRIX["member"]}

    new_ids = [perm_ids[k] for k in member_keys if k != "reminder.approve"]
    response = await roles_client.put(
        f"/agencies/me/roles/{member_role.id}/permissions",
        headers=agent_headers(admin),
        json={"permission_ids": new_ids},
    )
    assert response.status_code == 200
    clone = response.json()
    assert clone["id"] != str(member_role.id)
    assert clone["is_system"] is False
    assert clone["cloned_from_role_id"] == str(member_role.id)
    assert clone["name"] == "member"
    assert {p["key"] for p in clone["permissions"]} == member_keys - {"reminder.approve"}

    # The wearer was rebound to the clone...
    await db_session.refresh(wearer)
    assert wearer.role_id == uuid.UUID(clone["id"])
    # ...and the system role itself is UNTOUCHED in base.
    perm_keys = (
        await db_session.execute(
            text(
                "SELECT p.key FROM role_permission rp JOIN permission p "
                "ON p.id = rp.permission_id WHERE rp.role_id = :rid"
            ),
            {"rid": str(member_role.id)},
        )
    ).scalars()
    assert set(perm_keys) == member_keys


async def test_cow_second_edit_reuses_the_clone(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    member_role = system_roles["member"]
    first = await roles_client.put(
        f"/agencies/me/roles/{member_role.id}/permissions",
        headers=headers,
        json={"permission_ids": [perm_ids["case.view"]]},
    )
    assert first.status_code == 200
    clone_id = first.json()["id"]

    # Second edit of the SYSTEM id -> lands on the SAME clone, no re-clone.
    second = await roles_client.patch(
        f"/agencies/me/roles/{member_role.id}", headers=headers, json={"name": "collaborateur"}
    )
    assert second.status_code == 200
    assert second.json()["id"] == clone_id
    assert second.json()["name"] == "collaborateur"
    # The rename did not break the mask (FK link, not name matching).
    listing = await roles_client.get("/agencies/me/roles", headers=headers)
    ids = {r["id"] for r in listing.json()}
    assert clone_id in ids and str(member_role.id) not in ids


async def test_cow_delete_clone_rebinds_to_system_origin(
    roles_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    member_role = system_roles["member"]
    wearer = await make_agent(agency_id=admin.agency_id, role=member_role)
    edited = await roles_client.put(
        f"/agencies/me/roles/{member_role.id}/permissions",
        headers=headers,
        json={"permission_ids": [perm_ids["case.view"]]},
    )
    clone_id = edited.json()["id"]
    await db_session.refresh(wearer)
    assert wearer.role_id == uuid.UUID(clone_id)

    response = await roles_client.delete(f"/agencies/me/roles/{clone_id}", headers=headers)
    assert response.status_code == 200
    await db_session.refresh(wearer)
    assert wearer.role_id == member_role.id  # back on the system original

    listing = await roles_client.get("/agencies/me/roles", headers=headers)
    ids = {r["id"] for r in listing.json()}
    assert str(member_role.id) in ids and clone_id not in ids  # unmasked


async def test_cow_masking_in_listing_and_assignment(
    roles_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    member_role = system_roles["member"]
    edited = await roles_client.put(
        f"/agencies/me/roles/{member_role.id}/permissions",
        headers=headers,
        json={"permission_ids": [perm_ids["case.view"]]},
    )
    clone_id = edited.json()["id"]

    # Listing: the clone masks its origin for THIS agency...
    listing = await roles_client.get("/agencies/me/roles", headers=headers)
    ids = {r["id"] for r in listing.json()}
    assert clone_id in ids and str(member_role.id) not in ids

    # ...assignment of the masked system role is refused, naming the clone...
    colleague = await make_agent(agency_id=admin.agency_id)
    assign = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(member_role.id)},
    )
    assert assign.status_code == 409
    assert clone_id in assign.json()["detail"]

    # ...and another agency still sees and assigns the system role freely.
    other_agency = await make_agency()
    other_admin = await make_agent(agency_id=other_agency.id, role=system_roles["admin"])
    other_listing = await roles_client.get("/agencies/me/roles", headers=agent_headers(other_admin))
    assert str(member_role.id) in {r["id"] for r in other_listing.json()}


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
    # An explicit duplicate is NOT a copy-on-write clone: no mask.
    assert body["cloned_from_role_id"] is None
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
    """Without the ceiling the bypass is: create an inflated role and
    assign it - or here, directly assign the system admin role."""
    colleague = await make_agent(agency_id=limited_manager.agency_id)
    response = await roles_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=agent_headers(limited_manager),
        json={"role_id": str(system_roles["admin"].id)},
    )
    assert response.status_code == 403
    assert "ceiling" in response.json()["detail"].lower()


async def test_set_own_role_403(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    response = await roles_client.put(
        f"/agencies/me/members/{admin.id}/role",
        headers=agent_headers(admin),
        json={"role_id": str(system_roles["admin"].id)},
    )
    assert response.status_code == 403


async def test_assignment_takes_effect_immediately(
    roles_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """No cache anywhere: the very next request runs with the new role."""
    target = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    before = await roles_client.get("/auth/agent/me", headers=agent_headers(target))
    assert "case.edit" not in before.json()["effective_permissions"]

    response = await roles_client.put(
        f"/agencies/me/members/{target.id}/role",
        headers=agent_headers(admin),
        json={"role_id": str(system_roles["case_manager"].id)},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "case_manager"

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
    """Changing the only admin-ROLE holder's role is fine when another
    agent carries agent.manage via a custom role (the capability
    counts, not the role)."""
    agency = await make_agency()
    sole_admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    custom_manager = await make_role(
        permissions=list(LIMITED_CEILING), agency_id=agency.id, name="ops"
    )
    actor = await make_agent(agency_id=agency.id, role=custom_manager)
    response = await roles_client.put(
        f"/agencies/me/members/{sole_admin.id}/role",
        headers=agent_headers(actor),
        json={"role_id": str(system_roles["viewer"].id)},
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
    holder wears a custom role and edits its matrix, dropping
    agent.manage - refused, the agency would be unrecoverable."""
    agency = await make_agency()
    solo_manager_role = await make_role(
        permissions=[Permission.AGENT_MANAGE, Permission.ROLE_MANAGE, Permission.CASE_VIEW],
        agency_id=agency.id,
        name="solo-manager",
    )
    actor = await make_agent(agency_id=agency.id, role=solo_manager_role)
    response = await roles_client.put(
        f"/agencies/me/roles/{solo_manager_role.id}/permissions",
        headers=agent_headers(actor),
        json={"permission_ids": [perm_ids["role.manage"], perm_ids["case.view"]]},
    )
    assert response.status_code == 409
    assert "without any manager" in response.json()["detail"]


async def test_lockout_via_clone_deletion_409(
    roles_client: AsyncClient,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """Deleting a clone falls wearers back to the system matrix - if
    that drops the agency's last agent.manage, refuse."""
    agency = await make_agency()
    actor = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    helper = await make_agent(agency_id=agency.id, role=system_roles["admin"])

    # COW-edit 'member' to ADD agent.manage, then move the managers onto it.
    member_keys = [perm_ids[p.value] for p in SYSTEM_ROLE_MATRIX["member"]]
    edited = await roles_client.put(
        f"/agencies/me/roles/{system_roles['member'].id}/permissions",
        headers=agent_headers(actor),
        json={"permission_ids": [*member_keys, perm_ids["agent.manage"], perm_ids["role.manage"]]},
    )
    clone_id = edited.json()["id"]
    moved = await roles_client.put(
        f"/agencies/me/members/{helper.id}/role",
        headers=agent_headers(actor),
        json={"role_id": clone_id},
    )
    assert moved.status_code == 200
    demoted = await roles_client.put(
        f"/agencies/me/members/{actor.id}/role",
        headers=agent_headers(helper),
        json={"role_id": str(system_roles["viewer"].id)},
    )
    assert demoted.status_code == 200

    # The only manager now wears the clone; deleting it would rebind them
    # to plain 'member' (no agent.manage) -> lockout refused.
    response = await roles_client.delete(
        f"/agencies/me/roles/{clone_id}", headers=agent_headers(helper)
    )
    assert response.status_code == 409
    assert "without any manager" in response.json()["detail"]


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
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(foreign_role.id)},
    )
    assert assign_foreign_role.status_code == 422

    # Setting the role of an agent from another agency: 404.
    assign_foreign_agent = await roles_client.put(
        f"/agencies/me/members/{foreign_agent.id}/role",
        headers=headers,
        json={"role_id": str(foreign_role.id)},
    )
    assert assign_foreign_agent.status_code == 404


# --- GET /agencies/me/roles/{role_id} (the read mirror) ----------------------------


async def test_get_system_role_matrix(
    roles_client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    source = system_roles["case_manager"]
    response = await roles_client.get(
        f"/agencies/me/roles/{source.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(source.id)
    assert body["is_system"] is True
    expected = {p.value for p in SYSTEM_ROLE_MATRIX["case_manager"]}
    assert {p["key"] for p in body["permissions"]} == expected


async def test_get_custom_role_matrix(
    roles_client: AsyncClient,
    admin: Agent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    role = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.DOCUMENT_VALIDATE],
        agency_id=admin.agency_id,
        name="doc-reader",
    )
    response = await roles_client.get(f"/agencies/me/roles/{role.id}", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "doc-reader"
    assert body["is_system"] is False
    assert {p["key"] for p in body["permissions"]} == {"case.view", "document.validate"}


async def test_get_foreign_custom_role_404(
    roles_client: AsyncClient,
    admin: Agent,
    make_agency: MakeAgency,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    other_agency = await make_agency()
    foreign_role = await make_role(permissions=[Permission.CASE_VIEW], agency_id=other_agency.id)
    response = await roles_client.get(
        f"/agencies/me/roles/{foreign_role.id}", headers=agent_headers(admin)
    )
    assert response.status_code == 404


async def test_get_role_requires_role_manage(
    roles_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    response = await roles_client.get(
        f"/agencies/me/roles/{system_roles['member'].id}", headers=agent_headers(member)
    )
    assert response.status_code == 403


# --- migration backfill semantics ---------------------------------------------------


async def test_migration_backfill_picks_most_privileged_role(
    db_session: AsyncSession,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
) -> None:
    """Runs the EXACT backfill statement of migration d16404291a1d
    against a reconstructed agent_role M2M: an agent holding several
    roles keeps the most privileged one (permission count DESC orders
    the system roles admin > case_manager > member > viewer and slots
    customs by matrix size), and no agent is lost."""
    multi = await make_agent(role=system_roles["viewer"])
    small_custom = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.CASE_EDIT], agency_id=multi.agency_id
    )

    await db_session.execute(
        text(
            "CREATE TABLE agent_role ("
            "agent_id UUID NOT NULL REFERENCES agent(id), "
            "role_id UUID NOT NULL REFERENCES role(id), "
            "PRIMARY KEY (agent_id, role_id))"
        )
    )
    for role_id in (
        system_roles["viewer"].id,
        system_roles["case_manager"].id,
        small_custom.id,
        system_roles["member"].id,
    ):
        await db_session.execute(
            text("INSERT INTO agent_role (agent_id, role_id) VALUES (:a, :r)"),
            {"a": str(multi.id), "r": str(role_id)},
        )

    # The migration's backfill statement, verbatim.
    await db_session.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    ar.agent_id,
                    ar.role_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY ar.agent_id
                        ORDER BY
                            (SELECT COUNT(*) FROM role_permission rp
                              WHERE rp.role_id = r.id) DESC,
                            r.is_system DESC,
                            r.created_at,
                            r.id
                    ) AS rn
                FROM agent_role ar
                JOIN role r ON r.id = ar.role_id
            )
            UPDATE agent
            SET role_id = ranked.role_id
            FROM ranked
            WHERE ranked.agent_id = agent.id AND ranked.rn = 1
            """
        )
    )
    await db_session.execute(text("DROP TABLE agent_role"))
    await db_session.commit()

    await db_session.refresh(multi)
    # case_manager (largest matrix among the held roles) wins.
    assert multi.role_id == system_roles["case_manager"].id
