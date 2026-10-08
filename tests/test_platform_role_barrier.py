"""The platform barrier in the agency flows (escalation closed on 08/10/2026).

The probe of 08/10/2026: an agency admin who knew the id of the `superadmin`
system role renamed it (PATCH 200: the copy-on-write clone carried
agency.create, job.manage and platform.task_manage into the agency), invited
someone with the clone (POST 201: no ceiling, and a check on the role NAME
that the renamed clone escaped), the invitee accepted, and GET /jobs answered
200.

What holds now, whoever acts (a superadmin in its home agency included):
- the platform role is out of reach of every agency door, recognised by its
  identity (system row, no agency, reserved name), never by a name: it
  answers exactly like an unknown role (404 role.not_found on the role
  screens, 422 on the member role and on the invitation);
- no agency role carries a platform permission: matrix, clone, duplicate,
  assignment and invitation are capped by a ceiling that never holds one,
  and the boot never propagates one to a clone;
- a clone of the platform role left by the hole is an ordinary agency role:
  deleting it never lands its wearers on the platform role;
- a platform operator is never an impersonation target, and an invitation
  carrying the platform role is never accepted.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.rbac.baseline import (
    PLATFORM_PERMISSIONS,
    SYSTEM_ROLE_MATRIX,
    collect_bindings,
    seed_rbac_baseline,
)
from src.core.rbac.permissions import Permission
from src.jobs.jobs_baseline import seed_job_configs
from tests.plugins.agency_plugin import MakeAgency, MakeAgentInvitation
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.rbac_plugin import MakeRole

PLATFORM_KEYS = {p.value for p in PLATFORM_PERMISSIONS}
ROLE_NOT_FOUND = {"detail": "Role not found.", "code": "role.not_found", "params": {}}
# Every door of the role screens that takes a role id: (method, url, body).
ROLE_DOORS: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/agencies/me/roles/{id}", None),
    ("PATCH", "/agencies/me/roles/{id}", {"name": "Equipe"}),
    ("PUT", "/agencies/me/roles/{id}/permissions", {"permission_ids": []}),
    ("DELETE", "/agencies/me/roles/{id}", None),
    ("POST", "/agencies/me/roles/{id}/duplicate", {"name": "Copie"}),
]
# A manager who may invite and owns the whole system `member` matrix, but
# not the admin's: the gap the ceiling tests probe.
MEMBER_MANAGER = (Permission.AGENT_MANAGE, *SYSTEM_ROLE_MATRIX["member"])


@pytest_asyncio.fixture
async def barrier_client(
    client: AsyncClient, rbac_baseline: None, db_session: AsyncSession
) -> AsyncClient:
    await seed_job_configs(db_session)
    return client


@pytest_asyncio.fixture
async def perm_ids(rbac_baseline: None, db_session: AsyncSession) -> dict[str, str]:
    rows = (await db_session.execute(select(PermissionRow))).scalars().all()
    return {row.key: str(row.id) for row in rows}


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def operator(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """A platform operator (the system superadmin) in its home agency."""
    return await make_agent(role=system_roles["superadmin"])


async def _keys_of(db: AsyncSession, role_id: uuid.UUID) -> set[str]:
    stmt = (
        select(PermissionRow.key)
        .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
        .where(RolePermission.role_id == role_id)
    )
    return set((await db.execute(stmt)).scalars())


async def _clone_left_by_the_hole(
    make_role: MakeRole, superadmin: Role, agency_id: uuid.UUID, name: str = "Equipe"
) -> Role:
    """A clone of the platform role as the probe left it: an agency row
    carrying the whole superadmin matrix, platform permissions included."""
    return await make_role(
        permissions=list(SYSTEM_ROLE_MATRIX["superadmin"]),
        agency_id=agency_id,
        name=name,
        cloned_from_role_id=superadmin.id,
    )


async def _invite(
    client: AsyncClient, headers: dict[str, str], role_id: uuid.UUID | str
) -> tuple[int, dict[str, object]]:
    response = await client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": f"invitee-{uuid.uuid4().hex[:8]}@example.com", "role_id": str(role_id)},
    )
    return response.status_code, response.json()


# --- the probe of 08/10/2026 --------------------------------------------------------


async def test_the_probe_stops_at_its_first_step(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The exact chain of the probe. Its first step, the rename that minted
    the clone, answers like an unknown role and creates nothing; the
    invitation it would have sent next has no role to ride on."""
    superadmin_id = system_roles["superadmin"].id
    headers = agent_headers(admin)

    rename = await barrier_client.patch(
        f"/agencies/me/roles/{superadmin_id}", headers=headers, json={"name": "Equipe"}
    )
    assert rename.status_code == 404, rename.text
    assert rename.json() == ROLE_NOT_FOUND
    clones = await db_session.execute(
        select(Role.id).where(Role.cloned_from_role_id == superadmin_id)
    )
    assert clones.scalars().all() == []

    status, body = await _invite(barrier_client, headers, superadmin_id)
    assert (status, body["code"]) == (422, "invitation.role_invalid")
    assert (await db_session.execute(select(AgentInvitation.id))).scalars().all() == []
    jobs = await barrier_client.get("/jobs", headers=headers)
    assert jobs.status_code == 403  # the admin itself never held job.manage


async def test_the_clone_the_probe_left_opens_no_invitation(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    operator: Agent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Steps 2 to 4 of the probe, with a clone already in the agency: the
    invitation is capped by the inviter's ceiling, which never holds a
    platform permission, an operator inviting in its home agency included.
    The admin's refusal names the clone's Email keys too: since 08/10/2026
    only the platform grants those, and this admin was granted none."""
    email = "email.enroll, email.manage, email.pause, email.remove, email.sync, email.view, "
    for actor, also_missing in ((admin, email), (operator, "")):
        clone = await _clone_left_by_the_hole(
            make_role, system_roles["superadmin"], actor.agency_id
        )
        status, body = await _invite(barrier_client, agent_headers(actor), clone.id)
        assert status == 403, body
        assert body == {
            "detail": "Beyond your permission ceiling: agency.create, "
            f"{also_missing}job.manage, platform.task_manage.",
            "code": "role.beyond_ceiling",
            "params": {},
        }
    assert (await db_session.execute(select(AgentInvitation.id))).scalars().all() == []


# --- the platform role is out of reach, whoever acts --------------------------------


@pytest.mark.parametrize("actor_role", ["admin", "superadmin"])
async def test_the_platform_role_answers_like_an_unknown_role_on_every_role_door(
    actor_role: str,
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Read, rename, matrix edit, delete, duplicate: the platform role gets
    the very answer of a random id, for an agency admin and for an operator
    in its home agency alike. Nothing moves: no clone, the system row and
    the operator's own role untouched."""
    superadmin = system_roles["superadmin"]
    actor = await make_agent(role=system_roles[actor_role])
    headers = agent_headers(actor)
    for method, template, body in ROLE_DOORS:
        platform = await barrier_client.request(
            method, template.format(id=superadmin.id), headers=headers, json=body
        )
        unknown = await barrier_client.request(
            method, template.format(id=uuid.uuid4()), headers=headers, json=body
        )
        assert (platform.status_code, unknown.status_code) == (404, 404), (method, platform.text)
        assert platform.json() == unknown.json() == ROLE_NOT_FOUND

    clones = await db_session.execute(
        select(Role.id).where(Role.cloned_from_role_id == superadmin.id)
    )
    assert clones.scalars().all() == []
    assert await _keys_of(db_session, superadmin.id) == {
        p.value for p in SYSTEM_ROLE_MATRIX["superadmin"]
    }
    await db_session.refresh(actor)
    assert actor.role_id == system_roles[actor_role].id


@pytest.mark.parametrize("actor_role", ["admin", "superadmin"])
async def test_the_platform_role_is_neither_assignable_nor_invitable(
    actor_role: str,
    barrier_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The member-role and invitation doors answer the platform role exactly
    like an unknown role (their 422), an operator included."""
    actor = await make_agent(role=system_roles[actor_role])
    colleague = await make_agent(agency_id=actor.agency_id, role=system_roles["member"])
    headers = agent_headers(actor)
    url = f"/agencies/me/members/{colleague.id}/role"
    platform = await barrier_client.put(
        url, headers=headers, json={"role_id": str(system_roles["superadmin"].id)}
    )
    unknown = await barrier_client.put(url, headers=headers, json={"role_id": str(uuid.uuid4())})
    assert (platform.status_code, unknown.status_code) == (422, 422), platform.text
    assert platform.json() == unknown.json()
    assert platform.json()["code"] == "role.not_found"

    invite_platform = await _invite(barrier_client, headers, system_roles["superadmin"].id)
    invite_unknown = await _invite(barrier_client, headers, uuid.uuid4())
    assert invite_platform == invite_unknown
    assert invite_platform[0] == 422
    assert invite_platform[1]["code"] == "invitation.role_invalid"


async def test_an_agency_role_named_superadmin_is_an_ordinary_role(
    barrier_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    agent_headers: AuthHeaders,
) -> None:
    """Identity, never a name: a custom role the agency happened to name
    "superadmin" is read, assigned and invited with like any of its roles."""
    homonym = await make_role(
        permissions=[Permission.CASE_VIEW], agency_id=admin.agency_id, name="superadmin"
    )
    colleague = await make_agent(agency_id=admin.agency_id)
    headers = agent_headers(admin)

    read = await barrier_client.get(f"/agencies/me/roles/{homonym.id}", headers=headers)
    assert read.status_code == 200, read.text
    assign = await barrier_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(homonym.id)},
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["role"] == "superadmin"
    status, body = await _invite(barrier_client, headers, homonym.id)
    assert status == 201, body


# --- the ceiling: no agency role ever carries a platform permission ------------------


async def test_the_invitation_is_capped_like_the_member_role(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """One rule on both doors: nobody hands out, by invitation or by
    assignment, a role carrying a permission they do not hold. Within the
    ceiling, the invitation goes out as before."""
    agency = await make_agency(plan="agence", converted_at=datetime.now(UTC))
    ceiling = await make_role(
        permissions=list(MEMBER_MANAGER), agency_id=agency.id, name="team-lead"
    )
    manager = await make_agent(agency_id=agency.id, role=ceiling)
    colleague = await make_agent(agency_id=agency.id)
    headers = agent_headers(manager)

    status, invite = await _invite(barrier_client, headers, system_roles["admin"].id)
    assign = await barrier_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["admin"].id)},
    )
    assert (status, assign.status_code) == (403, 403), invite
    assert invite == assign.json()
    assert invite["code"] == "role.beyond_ceiling"
    assert "agency.manage" in str(invite["detail"])
    rows = await db_session.execute(select(AgentInvitation.id))
    assert rows.scalars().all() == []

    for within in (system_roles["member"].id, system_roles["viewer"].id, ceiling.id):
        status, body = await _invite(barrier_client, headers, within)
        assert status == 201, body


async def test_an_operator_hands_out_no_platform_permission_in_its_agency(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    operator: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    """The operator holds the platform permissions, yet acting through the
    agency flows of its home agency it puts none on an agency role: not in a
    new matrix, not in an edit, not by duplicating or assigning a clone of
    the platform role. The refusal names the platform keys."""
    headers = agent_headers(operator)
    own = await make_role(permissions=[Permission.CASE_VIEW], agency_id=operator.agency_id)
    clone = await _clone_left_by_the_hole(make_role, system_roles["superadmin"], operator.agency_id)
    colleague = await make_agent(agency_id=operator.agency_id, role=system_roles["member"])
    for key in sorted(PLATFORM_KEYS):
        asked = [perm_ids["case.view"], perm_ids[key]]
        create = await barrier_client.post(
            "/agencies/me/roles",
            headers=headers,
            json={"name": f"ops {key}", "permission_ids": asked},
        )
        edit = await barrier_client.put(
            f"/agencies/me/roles/{own.id}/permissions",
            headers=headers,
            json={"permission_ids": asked},
        )
        for response in (create, edit):
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "role.beyond_ceiling"
            assert response.json()["detail"] == f"Beyond your permission ceiling: {key}."

    duplicate = await barrier_client.post(
        f"/agencies/me/roles/{clone.id}/duplicate", headers=headers, json={"name": "Copie"}
    )
    assign = await barrier_client.put(
        f"/agencies/me/members/{colleague.id}/role",
        headers=headers,
        json={"role_id": str(clone.id)},
    )
    for response in (duplicate, assign):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "role.beyond_ceiling"

    assert await _keys_of(db_session, own.id) == {"case.view"}
    agency_roles = await db_session.execute(
        select(Role.id).where(Role.agency_id == operator.agency_id, Role.id != clone.id)
    )
    for role_id in agency_roles.scalars():
        assert (await _keys_of(db_session, role_id)).isdisjoint(PLATFORM_KEYS)
    await db_session.refresh(colleague)
    assert colleague.role_id == system_roles["member"].id


async def test_a_copy_on_write_clone_never_copies_a_platform_permission(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Defense in depth: a system role carrying a platform permission (a
    corrupted row; the seed never grants one) still clones without it."""
    member_id = system_roles["member"].id
    job_manage = await db_session.execute(
        select(PermissionRow.id).where(PermissionRow.key == Permission.JOB_MANAGE.value)
    )
    db_session.add(RolePermission(role_id=member_id, permission_id=job_manage.scalar_one()))
    await db_session.commit()

    rename = await barrier_client.patch(
        f"/agencies/me/roles/{member_id}", headers=agent_headers(admin), json={"name": "Equipe"}
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["cloned_from_role_id"] == str(member_id)
    assert {p["key"] for p in rename.json()["permissions"]} == {
        p.value for p in SYSTEM_ROLE_MATRIX["member"]
    }
    assert "job.manage" not in await _keys_of(db_session, uuid.UUID(rename.json()["id"]))


async def test_the_boot_never_propagates_a_platform_permission_to_a_clone(
    db_session: AsyncSession,
    admin: Agent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
) -> None:
    """The boot fills a clone with what its origin gained after the clone's
    last matrix decision. A clone of the platform role whose decision
    predates the platform permissions would receive them at every boot,
    undoing the cleanup migration: it never does. The birth-date rule still
    fills an internal permission."""
    superadmin_id = system_roles["superadmin"].id
    cleaned = [
        p
        for p in SYSTEM_ROLE_MATRIX["superadmin"]
        if p.value not in PLATFORM_KEYS and p is not Permission.CASE_VIEW
    ]
    clone = await make_role(
        permissions=cleaned,
        agency_id=admin.agency_id,
        name="Equipe",
        cloned_from_role_id=superadmin_id,
    )
    clone_id = clone.id
    await db_session.execute(
        update(Role)
        .where(Role.id == clone_id)
        .values(permissions_reviewed_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    await db_session.commit()

    await seed_rbac_baseline(db_session, bindings=collect_bindings())  # the next boot
    db_session.expire_all()
    keys = await _keys_of(db_session, clone_id)
    assert keys.isdisjoint(PLATFORM_KEYS)
    assert "case.view" in keys  # born after 2020: ignorance, filled
    superadmin_keys = await _keys_of(db_session, superadmin_id)
    assert superadmin_keys >= PLATFORM_KEYS


# --- the clone left by the hole, the acceptance, the impersonation -------------------


async def test_deleting_a_clone_of_the_platform_role_never_lands_on_it(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Deleting a clone hands its wearers back to the clone's origin. For a
    clone of the platform role that origin is out of reach: the clone is an
    ordinary agency role, refused while worn, deleted once free."""
    headers = agent_headers(admin)
    clone = await _clone_left_by_the_hole(make_role, system_roles["superadmin"], admin.agency_id)
    wearer = await make_agent(agency_id=admin.agency_id, role=clone)

    in_use = await barrier_client.delete(f"/agencies/me/roles/{clone.id}", headers=headers)
    assert in_use.status_code == 409, in_use.text
    assert in_use.json()["code"] == "role.in_use"
    await db_session.refresh(wearer)
    assert wearer.role_id == clone.id

    moved = await barrier_client.put(
        f"/agencies/me/members/{wearer.id}/role",
        headers=headers,
        json={"role_id": str(system_roles["member"].id)},
    )
    assert moved.status_code == 200, moved.text
    deleted = await barrier_client.delete(f"/agencies/me/roles/{clone.id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert await db_session.get(Role, clone.id) is None
    holders = await db_session.execute(
        select(Agent.id).where(Agent.role_id == system_roles["superadmin"].id)
    )
    assert holders.scalars().all() == []


async def test_an_invitation_carrying_the_platform_role_is_never_accepted(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent_invitation: MakeAgentInvitation,
    system_roles: dict[str, Role],
) -> None:
    """Belt for a legacy row: no door creates such an invitation, and one
    found in the table never turns its holder into a platform operator. It
    answers like an unknown token."""
    agency = await make_agency()
    invitation = await make_agent_invitation(
        agency_id=agency.id, role_id=system_roles["superadmin"].id, email="legacy@example.com"
    )
    accept = await barrier_client.post(
        "/agencies/invitations/accept",
        json={
            "token": invitation.token,
            "password": "password123",
            "first_name": "Legacy",
            "last_name": "Row",
        },
    )
    assert accept.status_code == 400, accept.text
    assert accept.json()["code"] == "invitation.invalid"
    created = await db_session.execute(select(Agent.id).where(Agent.email == "legacy@example.com"))
    assert created.scalars().all() == []


async def test_a_platform_operator_is_never_an_impersonation_target(
    barrier_client: AsyncClient,
    operator: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The impersonation token carries the TARGET's permissions: an admin of
    the operators' home agency (agent.impersonate) who impersonated an
    operator would act with the platform permissions. The operator answers
    like an unknown member; a regular colleague stays impersonable."""
    home_admin = await make_agent(agency_id=operator.agency_id, role=system_roles["admin"])
    headers = agent_headers(home_admin)
    platform = await barrier_client.post(
        f"/agencies/me/members/{operator.id}/impersonate", headers=headers
    )
    unknown = await barrier_client.post(
        f"/agencies/me/members/{uuid.uuid4()}/impersonate", headers=headers
    )
    assert (platform.status_code, unknown.status_code) == (404, 404), platform.text
    assert platform.json() == unknown.json()

    colleague = await make_agent(agency_id=operator.agency_id, role=system_roles["member"])
    regular = await barrier_client.post(
        f"/agencies/me/members/{colleague.id}/impersonate", headers=headers
    )
    assert regular.status_code == 200, regular.text


# --- non-regression ----------------------------------------------------------------


async def test_the_legitimate_impersonations_still_work(
    barrier_client: AsyncClient,
    operator: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """The operator check reads the TARGET only: the operator's "Enter" (the
    agency switcher) still lands on the target agency's admin, an agency
    admin still impersonates an ordinary colleague, and an operator still
    impersonates an ordinary member of its home agency."""
    target_agency = await make_agency()
    target_admin = await make_agent(agency_id=target_agency.id, role=system_roles["admin"])
    colleague = await make_agent(agency_id=target_agency.id, role=system_roles["member"])
    home_member = await make_agent(agency_id=operator.agency_id, role=system_roles["member"])

    async def _me_through(issued: Response) -> dict[str, Any]:
        """/auth/agent/me as seen through the impersonation token issued."""
        assert issued.status_code == 200, issued.text
        token = issued.json()["access_token"]
        me = await barrier_client.get(
            "/auth/agent/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200, me.text
        body: dict[str, Any] = me.json()
        return body

    entered = await _me_through(
        await barrier_client.post(
            f"/agencies/{target_agency.id}/enter", headers=agent_headers(operator)
        )
    )
    assert entered["id"] == str(target_admin.id)
    assert entered["agency_id"] == str(target_agency.id)
    assert entered["impersonator"] == {
        "agent_id": str(operator.id),
        "first_name": operator.first_name,
        "last_name": operator.last_name,
    }

    as_colleague = await _me_through(
        await barrier_client.post(
            f"/agencies/me/members/{colleague.id}/impersonate",
            headers=agent_headers(target_admin),
        )
    )
    assert as_colleague["id"] == str(colleague.id)
    assert as_colleague["impersonator"]["agent_id"] == str(target_admin.id)

    as_home_member = await _me_through(
        await barrier_client.post(
            f"/agencies/me/members/{home_member.id}/impersonate",
            headers=agent_headers(operator),
        )
    )
    assert as_home_member["id"] == str(home_member.id)
    assert as_home_member["impersonator"]["agent_id"] == str(operator.id)


async def test_an_admin_still_manages_its_roles_clones_and_invitations(
    barrier_client: AsyncClient,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    perm_ids: dict[str, str],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(plan="agence", converted_at=datetime.now(UTC))
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"])
    wearer = await make_agent(agency_id=agency.id, role=system_roles["case_manager"])
    viewer = await make_agent(agency_id=agency.id, role=system_roles["viewer"])
    headers = agent_headers(admin)

    rename = await barrier_client.patch(
        f"/agencies/me/roles/{system_roles['case_manager'].id}",
        headers=headers,
        json={"name": "Gestionnaire"},
    )
    assert rename.status_code == 200, rename.text
    clone_id = rename.json()["id"]
    await db_session.refresh(wearer)
    assert str(wearer.role_id) == clone_id  # copy-on-write rebind
    edit = await barrier_client.put(
        f"/agencies/me/roles/{clone_id}/permissions",
        headers=headers,
        json={"permission_ids": [perm_ids["case.view"], perm_ids["case.edit"]]},
    )
    assert edit.status_code == 200, edit.text
    create = await barrier_client.post(
        "/agencies/me/roles",
        headers=headers,
        json={"name": "Lecteur", "permission_ids": [perm_ids["case.view"]]},
    )
    assert create.status_code == 201, create.text

    for role_id in (
        system_roles["admin"].id,
        system_roles["member"].id,
        clone_id,
        create.json()["id"],
    ):
        status, body = await _invite(barrier_client, headers, role_id)
        assert status == 201, body

    # Deleting a clone of an agency system role still unmasks onto it. (A
    # clone some pending invitation names is not deleted here: that delete
    # trips the agent_invitation foreign key, a separate matter.)
    viewer_clone = await barrier_client.patch(
        f"/agencies/me/roles/{system_roles['viewer'].id}",
        headers=headers,
        json={"name": "Observateur"},
    )
    assert viewer_clone.status_code == 200, viewer_clone.text
    unmask = await barrier_client.delete(
        f"/agencies/me/roles/{viewer_clone.json()['id']}", headers=headers
    )
    assert unmask.status_code == 200, unmask.text
    await db_session.refresh(viewer)
    assert viewer.role_id == system_roles["viewer"].id


async def test_the_operator_keeps_its_platform_gestures(
    barrier_client: AsyncClient,
    operator: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(operator)
    for url in ("/jobs", "/agencies", "/admin/tasks"):
        response = await barrier_client.get(url, headers=headers)
        assert response.status_code == 200, (url, response.text)
    me = await barrier_client.get("/auth/agent/me", headers=headers)
    assert set(me.json()["effective_permissions"]) >= PLATFORM_KEYS
